"""Realized volatility from observed, fixed-cadence game prices.

The chart currently has timestamped trades, but no timestamped bid/ask quotes.
We therefore use its trade-implied probability, taking the last *observed*
trade in each selected time bucket. Empty buckets are never forward-filled.
"""

from __future__ import annotations

from bisect import bisect_left
from math import sqrt

import pandas as pd


DEFAULT_CADENCE_SECONDS = 3
WINDOWS_SECONDS = (60, 300, 600, 1800)
# Wider windows can tolerate wider trade spacing without inventing prices
# in empty time buckets.
MAX_GAP_SECONDS = (30, 120, 180, 600)
START_TOLERANCE_SECONDS = (10, 60, 120, 360)


def rolling_volatility_payload(
    compiled: pd.DataFrame, *, cadence_seconds: int = DEFAULT_CADENCE_SECONDS
) -> dict:
    """Return as-of samples and window volatility in percentage points.

    A window is available only when an observed sample covers its beginning
    within its tolerance and no adjacent observations exceed its gap limit.
    Each returned timestamp is the actual last-trade time of its bucket,
    so selecting the last sample at or before a chart cursor cannot leak future
    observations from the same bucket.
    """
    if (
        isinstance(cadence_seconds, bool)
        or not isinstance(cadence_seconds, int)
        or not 1 <= cadence_seconds <= 60
    ):
        raise ValueError("volatility cadence must be an integer from 1 to 60 seconds")
    frame = compiled[["timestamp", "target_raw_prob"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    frame["target_raw_prob"] = pd.to_numeric(frame["target_raw_prob"], errors="coerce")
    frame = frame.dropna().sort_values("timestamp", kind="stable")
    frame = frame[frame["target_raw_prob"].between(0, 1)]
    buckets = frame["timestamp"].dt.floor(f"{cadence_seconds}s")
    frame = frame.loc[~buckets.duplicated(keep="last")]

    times = [int(value.value // 1_000_000) for value in frame["timestamp"]]
    prices = frame["target_raw_prob"].tolist()
    squared = [0.0]
    gaps = [[0] for _ in WINDOWS_SECONDS]
    for index in range(1, len(times)):
        squared.append(squared[-1] + (prices[index] - prices[index - 1]) ** 2)
        interval_ms = times[index] - times[index - 1]
        for gap_counts, limit in zip(gaps, MAX_GAP_SECONDS):
            gap_counts.append(gap_counts[-1] + int(interval_ms > limit * 1000))

    samples = []
    for index, timestamp in enumerate(times):
        values = []
        per_minute = []
        reasons = []
        for window_index, seconds in enumerate(WINDOWS_SECONDS):
            cutoff = timestamp - seconds * 1000
            first = bisect_left(times, cutoff)
            if first == index or times[first] > cutoff + START_TOLERANCE_SECONDS[window_index] * 1000:
                values.append(None)
                per_minute.append(None)
                reasons.append("insufficient window coverage")
            elif gaps[window_index][index] != gaps[window_index][first]:
                values.append(None)
                per_minute.append(None)
                reasons.append("data gap / unsupported resolution")
            else:
                realized_pp = 100 * sqrt(max(0.0, squared[index] - squared[first]))
                values.append(round(realized_pp, 6))
                per_minute.append(round(realized_pp * sqrt(60 / seconds), 6))
                reasons.append(None)
        samples.append({"t": timestamp, "values": values, "per_minute": per_minute, "reasons": reasons})

    return {
        "cadence_seconds": cadence_seconds,
        "max_gap_seconds": list(MAX_GAP_SECONDS),
        "start_tolerance_seconds": list(START_TOLERANCE_SECONDS),
        "windows_seconds": list(WINDOWS_SECONDS),
        "normalization_seconds": 60,
        "source": "Kalshi trade-implied probability (historical bid/ask midpoints unavailable)",
        "samples": samples,
    }
