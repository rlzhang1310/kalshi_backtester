"""Rolling movement metrics from a clock-sampled target-side price series.

Trades provide the chart's price history; timestamped bid/ask history is not
available. Samples carry the last known price forward, never backward before
the first trade. The same helper serves historical and both live UI modes.
"""

from __future__ import annotations

from math import sqrt

import numpy as np
import pandas as pd


DEFAULT_CADENCE_SECONDS = 3
WINDOWS_SECONDS = (60, 300, 600, 1800)
PRICE_SOURCE = (
    "Kalshi trade-implied target probability; "
    "timestamped bid/ask history unavailable"
)


def _movement_metrics(prices: np.ndarray, seconds: int) -> dict[str, float]:
    """Return accumulated metrics and their 1-minute equivalents."""
    differences = np.diff(prices)
    upward = differences[differences > 0]
    downward = differences[differences < 0]
    up_squared = 10_000 * float(np.dot(upward, upward))
    down_squared = 10_000 * float(np.dot(downward, downward))
    two_way_squared = 2 * min(up_squared, down_squared)
    minute_factor = sqrt(60 / seconds)
    volatility = sqrt(up_squared + down_squared)
    up = sqrt(up_squared)
    down = sqrt(down_squared)
    two_way = sqrt(two_way_squared)
    return {
        "values": round(volatility, 6),
        "per_minute": round(volatility * minute_factor, 6),
        "up": round(up, 6),
        "down": round(down, 6),
        "two_way": round(two_way, 6),
        "up_per_minute": round(up * minute_factor, 6),
        "down_per_minute": round(down * minute_factor, 6),
        "two_way_per_minute": round(two_way * minute_factor, 6),
    }


def rolling_volatility_payload(
    compiled: pd.DataFrame,
    *,
    cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
    as_of: pd.Timestamp | int | None = None,
) -> dict:
    """Compute realized movement metrics as of a time, using no future trades.

    Integer ``as_of`` values are UTC epoch milliseconds. With ``None``, the
    latest observed trade is the as-of time. Only the longest window of
    sampled prices is materialized; the full-game path keeps only bucket-end
    trade prices, so long quiet periods do not expand memory use.
    """
    if (
        isinstance(cadence_seconds, bool)
        or not isinstance(cadence_seconds, int)
        or not 1 <= cadence_seconds <= 60
    ):
        raise ValueError("price metric cadence must be an integer from 1 to 60 seconds")

    frame = compiled[["timestamp", "target_raw_prob"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["target_raw_prob"] = pd.to_numeric(frame["target_raw_prob"], errors="coerce")
    frame = frame.dropna().sort_values("timestamp", kind="stable")
    frame = frame[frame["target_raw_prob"].between(0, 1)]
    if as_of is None:
        as_of_time = frame["timestamp"].iloc[-1] if not frame.empty else None
    elif isinstance(as_of, bool):
        raise ValueError("as_of must be a UTC timestamp or epoch milliseconds")
    elif isinstance(as_of, int):
        as_of_time = pd.Timestamp(as_of, unit="ms", tz="UTC")
    else:
        as_of_time = pd.Timestamp(as_of)
        if as_of_time.tzinfo is None:
            as_of_time = as_of_time.tz_localize("UTC")
        else:
            as_of_time = as_of_time.tz_convert("UTC")

    payload = {
        "cadence_seconds": cadence_seconds,
        "windows_seconds": list(WINDOWS_SECONDS),
        "normalization_seconds": 60,
        "source": PRICE_SOURCE,
        "as_of_ms": int(as_of_time.value // 1_000_000) if as_of_time is not None else None,
        "last_trade_ms": None,
        "game": None,
        "samples": [],
    }
    if as_of_time is None:
        return payload
    frame = frame.loc[frame["timestamp"] <= as_of_time]
    if frame.empty:
        return payload

    trade_times = frame["timestamp"].astype("int64").to_numpy()
    trade_prices = frame["target_raw_prob"].to_numpy(dtype=float)
    first_ns = int(trade_times[0])
    cadence_ns = cadence_seconds * 1_000_000_000
    count = int((as_of_time.value - first_ns) // cadence_ns) + 1
    known_seconds = max(1, int((as_of_time.value - first_ns) // 1_000_000_000) + 1)
    longest_count = max(
        (window + cadence_seconds - 1) // cadence_seconds
        for window in WINDOWS_SECONDS
    )
    first_index = max(0, count - longest_count)
    indices = np.arange(first_index, count, dtype=np.int64)
    sample_times = as_of_time.value - (count - 1 - indices) * cadence_ns
    observed_indices = np.searchsorted(trade_times, sample_times, side="right") - 1
    prices = trade_prices[observed_indices]
    payload["last_trade_ms"] = int(trade_times[observed_indices[-1]] // 1_000_000)

    # A trade affects the sampled path at the first grid time at or after it.
    # Keep the last trade in each bucket; empty buckets carry forward.
    first_sample_ns = as_of_time.value - (count - 1) * cadence_ns
    buckets = (trade_times - first_sample_ns + cadence_ns - 1) // cadence_ns
    bucket_ends = np.r_[buckets[1:] != buckets[:-1], True]
    game_prices = trade_prices[bucket_ends]
    payload["game"] = {
        **_movement_metrics(game_prices, known_seconds),
        "coverage_seconds": known_seconds,
    }

    metric_names = (
        "values", "per_minute", "up",
        "down", "two_way", "up_per_minute",
        "down_per_minute", "two_way_per_minute",
    )
    rolling = {name: [] for name in metric_names}
    partial = []
    coverage_seconds = []
    for window in WINDOWS_SECONDS:
        requested_count = (window + cadence_seconds - 1) // cadence_seconds
        available_count = min(count, requested_count)
        window_prices = prices[-available_count:]
        effective_seconds = min(window, known_seconds)
        metrics = _movement_metrics(window_prices, effective_seconds)
        for name in metric_names:
            rolling[name].append(metrics[name])
        partial.append(known_seconds < window)
        coverage_seconds.append(effective_seconds)

    payload["samples"] = [
        {
            "t": int(sample_times[-1] // 1_000_000),
            **rolling,
            "partial": partial,
            "coverage_seconds": coverage_seconds,
        }
    ]
    return payload
