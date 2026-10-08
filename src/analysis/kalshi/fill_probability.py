"""Retrospective touch/trade-through estimates for a resting YES bid.

The two estimates are execution proxies from prints, not queue simulations.
Actual settlement is used to normalize time, so the clock is retrospective.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from decimal import Decimal
import re

import numpy as np
import pandas as pd


def normalize_family(value: str) -> str:
    family = value.strip().upper().removesuffix("-*").removesuffix("-")
    if not re.fullmatch(r"[A-Z0-9]+", family):
        raise ValueError(
            "Use a series family such as KXATPCHALLENGERMATCH (optionally ending in -*)."
        )
    return family


def utc(values):
    return pd.to_datetime(values, utc=True, errors="coerce", format="mixed")


def normalize_trade_prices(trades: pd.DataFrame) -> pd.DataFrame:
    """Repair stored cent prices using the same taker-side rule as the backtester."""
    required = {
        "trade_id",
        "ticker",
        "created_time",
        "yes_price",
        "no_price",
        "taker_side",
    }
    if missing := required.difference(trades.columns):
        raise ValueError(f"Trades are missing: {', '.join(sorted(missing))}")
    frame = trades.copy()
    frame["ticker"] = frame.ticker.astype(str).str.upper()
    frame["timestamp"] = utc(frame.created_time)
    side = frame.taker_side.str.lower()
    price = pd.to_numeric(frame.yes_price, errors="coerce").where(
        side.eq("yes"), pd.to_numeric(frame.no_price, errors="coerce")
    )
    frame["price_cents"] = price.where(side.eq("yes"), 100 - price)
    valid = side.isin(["yes", "no"]) & price.between(0, 100) & frame.timestamp.notna()
    if "is_block_trade" in frame:
        valid &= ~frame.is_block_trade.fillna(False).astype(bool)
    return (
        frame[valid]
        .drop_duplicates("trade_id")
        .sort_values(["ticker", "timestamp", "trade_id"], kind="stable")
        .reset_index(drop=True)
    )


def normalize_timings(timings: pd.DataFrame, family: str) -> pd.DataFrame:
    required = {"ticker", "event_ticker", "start_time", "settlement_time", "close_time"}
    if missing := required.difference(timings.columns):
        raise ValueError(f"Timings are missing: {', '.join(sorted(missing))}")
    frame = timings.copy()
    for column in ("ticker", "event_ticker"):
        frame[column] = frame[column].astype(str).str.upper()
    frame = frame[
        frame.event_ticker.str.startswith(normalize_family(family) + "-")
    ].copy()
    if frame.ticker.duplicated().any():
        raise ValueError("Timings must have exactly one row per market ticker.")
    for column in ("start_time", "settlement_time", "close_time"):
        frame[column] = utc(frame[column])
    return frame


def add_normalized_time(trades: pd.DataFrame, timings: pd.DataFrame) -> pd.DataFrame:
    """Keep pregame t<0 and post-settlement t>1 visible rather than clipping them."""
    frame = trades.merge(
        timings[
            ["ticker", "event_ticker", "start_time", "settlement_time", "close_time"]
        ],
        on="ticker",
        how="inner",
        validate="many_to_one",
    )
    duration = (frame.settlement_time - frame.start_time).dt.total_seconds()
    frame["normalized_time"] = (
        frame.timestamp - frame.start_time
    ).dt.total_seconds() / duration.where(duration > 0)
    frame["in_game"] = (
        (frame.timestamp >= frame.start_time)
        & (frame.timestamp < frame.close_time)
        & (frame.timestamp < frame.settlement_time)
    )
    return frame


@dataclass
class FillDataset:
    snapshots: pd.DataFrame
    audit: pd.DataFrame


SNAPSHOT_COLUMNS = [
    "ticker",
    "event_ticker",
    "clv",
    "current_price",
    "normalized_time",
    "observation_time",
    "price_time",
    "price_age_seconds",
    "clv_time",
    "clv_age_seconds",
    "start_time",
    "close_time",
    "settlement_time",
    "future_min_cents",
    "future_trade_count",
]


def build_fill_dataset(
    trades: pd.DataFrame,
    timings: pd.DataFrame,
    *,
    family: str,
    coverage_end: str | pd.Timestamp | Mapping[str, pd.Timestamp],
    time_step: float = 0.05,
    max_clv_age_minutes: float | None = None,
) -> FillDataset:
    """Sample each ticker at fixed normalized times, using strictly future prints.

    CLV is the latest observed YES price strictly before the scheduled start.
    Current price is as-of the observation. A bid rests until trading closes.
    Settlement value is never appended as an executable print.
    """
    if not np.isfinite(time_step) or not 0 < time_step <= 1 or time_step < 0.001:
        raise ValueError("time_step must be between 0.001 and 1.")
    if max_clv_age_minutes is not None and (
        not np.isfinite(max_clv_age_minutes) or max_clv_age_minutes < 0
    ):
        raise ValueError("max_clv_age_minutes must be nonnegative.")
    coverage_by_ticker = coverage_end if isinstance(coverage_end, Mapping) else None
    covered_through = None if coverage_by_ticker is not None else utc(coverage_end)
    if coverage_by_ticker is None and (not isinstance(covered_through, pd.Timestamp) or pd.isna(covered_through)):
        raise ValueError("coverage_end must be a valid timestamp.")
    metadata = normalize_timings(timings, family)
    clean = normalize_trade_prices(trades)
    grouped = {ticker: group for ticker, group in clean.groupby("ticker", sort=False)}
    grid = np.arange(0, 1, time_step)
    rows, audit = [], []
    for market in metadata.itertuples(index=False):
        market_covered_through = (
            utc(coverage_by_ticker.get(market.ticker))
            if coverage_by_ticker is not None else covered_through
        )
        if not isinstance(market_covered_through, pd.Timestamp) or pd.isna(market_covered_through):
            raise ValueError(f"Missing checked coverage end for {market.ticker}")
        entry = {
            "ticker": market.ticker,
            "event_ticker": market.event_ticker,
            "status": "included",
            "snapshots": 0,
        }
        start, settle, close = (
            market.start_time,
            market.settlement_time,
            market.close_time,
        )
        if pd.isna(start) or pd.isna(settle) or pd.isna(close):
            entry["status"] = "missing_timing"
        elif settle <= start or close <= start or close > settle:
            entry["status"] = "invalid_timing"
        elif settle > market_covered_through:
            entry["status"] = "incomplete_trade_horizon"
        elif getattr(market, "status", "finalized") not in ("finalized", "settled"):
            entry["status"] = "not_settled"
        frame = grouped.get(market.ticker)
        if entry["status"] != "included":
            audit.append(entry)
            continue
        if frame is None or frame.empty:
            entry["status"] = "no_trades"
            audit.append(entry)
            continue
        # Exclude trades at/after close. Payouts and later data cannot fill a bid.
        frame = frame[frame.timestamp < close]
        pregame = frame[frame.timestamp < start]
        if pregame.empty:
            entry["status"] = "no_pregame_price"
            audit.append(entry)
            continue
        clv_row = pregame.iloc[-1]
        clv_age = (start - clv_row.timestamp).total_seconds()
        if max_clv_age_minutes is not None and clv_age > max_clv_age_minutes * 60:
            entry["status"] = "stale_pregame_price"
            audit.append(entry)
            continue
        times = pd.DatetimeIndex(frame.timestamp).as_unit("ns").asi8
        prices = frame.price_cents.to_numpy(dtype=float)
        # One reverse cumulative minimum answers every possible bid y later.
        suffix_min = np.minimum.accumulate(prices[::-1])[::-1]
        for t in grid:
            observation = start + (settle - start) * float(t)
            if observation >= close:
                continue
            right = int(np.searchsorted(times, observation.value, side="right"))
            if right == 0:
                continue
            previous = frame.iloc[right - 1]
            rows.append(
                {
                    "ticker": market.ticker,
                    "event_ticker": market.event_ticker,
                    "clv": float(clv_row.price_cents) / 100,
                    "current_price": float(prices[right - 1]) / 100,
                    "normalized_time": float(t),
                    "observation_time": observation,
                    "price_time": previous.timestamp,
                    "price_age_seconds": (
                        observation - previous.timestamp
                    ).total_seconds(),
                    "clv_time": clv_row.timestamp,
                    "clv_age_seconds": clv_age,
                    "start_time": start,
                    "close_time": close,
                    "settlement_time": settle,
                    "future_min_cents": float(suffix_min[right])
                    if right < len(prices)
                    else np.nan,
                    "future_trade_count": len(prices) - right,
                }
            )
            entry["snapshots"] += 1
        audit.append(entry)
    return FillDataset(
        pd.DataFrame(rows, columns=SNAPSHOT_COLUMNS),
        pd.DataFrame(audit, columns=["ticker", "event_ticker", "status", "snapshots"]),
    )


def _probability(value: float, name: str) -> float:
    value = float(value)
    if not np.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a probability in [0, 1].")
    return value


def estimate_fill_probability(
    snapshots: pd.DataFrame,
    *,
    family: str,
    clv: float,
    current_price: float,
    normalized_time: float,
    bid_price: float,
    ticker: str | None = None,
    clv_tolerance: float = 0.05,
    price_tolerance: float = 0.05,
    time_tolerance: float = 0.025,
    min_events: int = 20,
    as_of: str | pd.Timestamp | None = None,
) -> dict:
    """Estimate conditional touch and trade-through rates from similar states.

    Every matching event gets equal weight, regardless of its trading volume
    or number of matching outcome/timestamp rows. The query's entire event is
    excluded. Set as_of for a chronological research cutoff.
    """
    family = normalize_family(family)
    clv = _probability(clv, "clv")
    current_price = _probability(current_price, "current_price")
    normalized_time = _probability(normalized_time, "normalized_time")
    bid_price = _probability(bid_price, "bid_price")
    if bid_price >= current_price:
        raise ValueError("bid_price y must be strictly below current_price x.")
    for name, value in (
        ("clv_tolerance", clv_tolerance),
        ("price_tolerance", price_tolerance),
        ("time_tolerance", time_tolerance),
    ):
        _probability(value, name)
    if not isinstance(min_events, int) or min_events < 1:
        raise ValueError("min_events must be a positive integer.")
    mask = snapshots.event_ticker.str.startswith(family + "-")
    mask &= snapshots.clv.sub(clv).abs() <= clv_tolerance + 1e-12
    mask &= snapshots.current_price.sub(current_price).abs() <= price_tolerance + 1e-12
    mask &= (
        snapshots.normalized_time.sub(normalized_time).abs() <= time_tolerance + 1e-12
    )
    # Keep the hypothetical order a bid below the observed price in every peer.
    mask &= snapshots.current_price > bid_price
    excluded_event = None
    if ticker:
        ticker = ticker.strip().upper()
        if not ticker.startswith(family + "-"):
            raise ValueError("ticker must belong to the selected family.")
        matches = snapshots.loc[snapshots.ticker.eq(ticker), "event_ticker"].unique()
        excluded_event = (
            str(matches[0]) if len(matches) else "-".join(ticker.split("-")[:2])
        )
        mask &= ~snapshots.event_ticker.eq(excluded_event)
    if as_of is not None:
        cutoff = utc(as_of)
        if not isinstance(cutoff, pd.Timestamp) or pd.isna(cutoff):
            raise ValueError("as_of must be a valid timestamp.")
        mask &= utc(snapshots.settlement_time) < cutoff
    matches = snapshots.loc[mask].copy()
    threshold = float(Decimal(str(bid_price)) * 100)
    matches["optimistic"] = matches.future_min_cents.le(threshold)
    matches["conservative"] = matches.future_min_cents.lt(threshold)
    event_rates = matches.groupby("event_ticker")[["optimistic", "conservative"]].mean()
    enough = len(event_rates) >= min_events
    optimistic = float(event_rates.optimistic.mean()) if enough else None
    conservative = float(event_rates.conservative.mean()) if enough else None
    result = {
        "family": family,
        "ticker": ticker,
        "clv": clv,
        "current_price": current_price,
        "normalized_time": normalized_time,
        "bid_price": bid_price,
        "p_conservative": conservative,
        "p_optimistic": optimistic,
        "fill_probability_gap": optimistic - conservative if enough else None,
        "n_events": len(event_rates),
        "n_tickers": int(matches.ticker.nunique()),
        "n_observations": len(matches),
        "min_events": min_events,
        "status": "ok" if enough else "insufficient_support",
        "clv_tolerance": clv_tolerance,
        "price_tolerance": price_tolerance,
        "time_tolerance": time_tolerance,
        "excluded_event": excluded_event,
        "as_of": str(as_of) if as_of is not None else None,
        "time_basis": "retrospective_start_to_settlement",
    }
    if normalized_time == 1:
        # An expired order cannot fill, irrespective of nearby historical states.
        result.update(
            p_conservative=0.0,
            p_optimistic=0.0,
            fill_probability_gap=0.0,
            n_events=0,
            n_tickers=0,
            n_observations=0,
            status="horizon_ended",
        )
    return result
