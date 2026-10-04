"""Interactive single-game Kalshi price visualization.

The public ``visualize_game_odds`` function accepts either a Kalshi event
ticker or one of that event's child market tickers. It loads local Parquet or
live/archived API trades, converts them to a target-side probability series,
and optionally adds sportsbook curves and a play-by-play track.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import pandas as pd
import plotly.graph_objects as go

from src.indexers.kalshi.client import KalshiClient
from src.indexers.kalshi.models import Market
from src.analysis.kalshi.local_game_data import DEFAULT_DATA_DIR, LocalGameStore
from src.analysis.kalshi.rolling_volatility import (
    DEFAULT_CADENCE_SECONDS,
    rolling_volatility_payload,
)


TAKER_FEE_RATE = 0.07
MAKER_FEE_RATE = 0.0175
MAX_RESAMPLED_POINTS = 2_000_000
VOLUME_TARGET_BARS = 120
LIVE_TRADE_OVERLAP_SECONDS = 60


class GameOddsClient(Protocol):
    """Client surface used by the visualizer, kept small for easy testing."""

    def get_market(self, ticker: str, historical: bool = False) -> Market | None: ...

    def get_event_markets(
        self,
        event_ticker: str,
        historical: bool = False,
        limit: int = 1000,
    ) -> list[Market]: ...

    def get_event_milestones(
        self,
        event_ticker: str,
        limit: int = 500,
    ) -> list[dict]: ...

    def get_market_trades_data(
        self,
        ticker: str,
        limit: int = 1000,
        verbose: bool = True,
        min_ts: int | None = None,
        max_ts: int | None = None,
        historical: bool = False,
    ) -> list[dict]: ...

    def close(self) -> None: ...


@dataclass
class GameOddsData:
    """Fetched and normalized data behind a single game chart."""

    event_ticker: str
    target_ticker: str
    target_side: str
    target_label: str
    start_time: pd.Timestamp
    start_time_source: str
    markets: tuple[Market, ...]
    trades: pd.DataFrame
    compiled: pd.DataFrame


def fee_probability(probability: pd.Series, fee_rate: float) -> pd.Series:
    """Return the notebook's probability-equivalent Kalshi fee estimate."""
    return fee_rate * probability * (1 - probability)


def discover_game_markets(
    ticker: str,
    client: GameOddsClient,
) -> tuple[str, list[Market], Market | None]:
    """Resolve an event or child market ticker to all markets in its event.

    Returns ``(event_ticker, markets, input_market)``. ``input_market`` is set
    when the supplied ticker was a child market, which lets the caller select
    that outcome automatically.
    """
    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("ticker cannot be empty")

    input_market = _find_market(client, normalized_ticker)
    event_ticker = (
        input_market.event_ticker.upper()
        if input_market is not None
        else normalized_ticker
    )
    markets = _get_complete_event_markets(client, event_ticker)
    if not markets:
        if input_market is None:
            raise ValueError(
                f"No Kalshi event or market was found for ticker {normalized_ticker!r}."
            )
        raise ValueError(
            f"Found market {normalized_ticker!r}, but no markets were returned "
            f"for its event {event_ticker!r}."
        )

    return event_ticker, markets, input_market


def compile_event_trades_to_target(
    trades: pd.DataFrame,
    target_ticker: str,
    target_side: str | None = None,
    *,
    prices_are_cents: bool = False,
) -> pd.DataFrame:
    """Convert both outcomes of a two-market event to one probability series.

    A trade in the selected market uses its YES price directly. A trade in the
    opposing market is expressed as ``1 - YES``. Input prices are decimal
    probabilities unless ``prices_are_cents=True`` is passed explicitly.
    """
    if trades.empty:
        return pd.DataFrame()

    required_columns = {"timestamp", "ticker", "yes_price"}
    missing = required_columns.difference(trades.columns)
    if missing:
        raise ValueError(
            "trades is missing required columns: " + ", ".join(sorted(missing))
        )

    selected_ticker = target_ticker.strip().upper()
    frame = trades.copy()
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")

    yes_price = pd.to_numeric(frame["yes_price"], errors="coerce")
    if prices_are_cents:
        yes_price = yes_price / 100
    frame["yes_price"] = yes_price

    invalid_prices = yes_price.notna() & ~yes_price.between(0, 1)
    if invalid_prices.any():
        raise ValueError(
            f"yes_price contains {int(invalid_prices.sum())} value(s) outside [0, 1]"
        )

    is_target = frame["ticker"].eq(selected_ticker)
    frame["target_raw_prob"] = yes_price.where(is_target, 1 - yes_price)
    frame["target_ticker"] = selected_ticker
    frame["target_side"] = target_side or selected_ticker.rsplit("-", 1)[-1]
    frame["taker_prob"] = frame["target_raw_prob"] + fee_probability(
        frame["target_raw_prob"], TAKER_FEE_RATE
    )
    frame["maker_prob"] = frame["target_raw_prob"] + fee_probability(
        frame["target_raw_prob"], MAKER_FEE_RATE
    )

    frame = frame.dropna(subset=["timestamp", "target_raw_prob"])
    if "trade_id" in frame.columns:
        frame = frame.drop_duplicates(subset=["trade_id"], keep="last")
        sort_columns = ["timestamp", "trade_id"]
    else:
        sort_columns = ["timestamp", "ticker"]
    return frame.sort_values(sort_columns, kind="stable").reset_index(drop=True)


def load_game_odds(
    ticker: str,
    *,
    target: str | None = None,
    start_time: str | datetime | pd.Timestamp | None = None,
    end_time: str | datetime | pd.Timestamp | None = None,
    client: GameOddsClient | None = None,
    source: str = "api",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    allow_empty: bool = False,
) -> GameOddsData:
    """Discover, fetch, and normalize the trades for one Kalshi game.

    ``ticker`` can be the event ticker or either child market ticker. When an
    event ticker is supplied, ``target`` may be a child ticker, outcome suffix,
    or YES subtitle. If omitted, the function uses the child market whose
    suffix appears at the end of the event ticker (the convention used by the
    reference sports-game notebook). With source='api', an omitted start uses
    Kalshi's linked sports milestone. With source='local', the entire stored
    trade history is used unless a start is provided; no API call is made.
    Events with more than two outcomes use only the selected market's trades.
    ``allow_empty`` permits an unsettled API game with no initial trades so a
    live UI can wait for its first price; CLI callers keep the error default.
    """
    requested_start = _to_utc_timestamp(start_time, "start_time")
    end = _to_utc_timestamp(end_time, "end_time")
    if requested_start is not None and end is not None and requested_start > end:
        raise ValueError("start_time must be before or equal to end_time")
    if source == "local":
        store = LocalGameStore(data_dir)
        event, markets, input_market = store.resolve_markets(ticker)
        _validate_game_markets(event, markets)
        selected = _select_target_market(
            event_ticker=event,
            markets=markets,
            requested_target=target,
            input_market=input_market,
        )
        # Only a two-outcome event permits using the opposite YES as 1-p.
        trade_markets = markets if len(markets) == 2 else [selected]
        trades = store.load_trades(
            [market.ticker for market in trade_markets],
            start=requested_start,
            end=end,
        )
        side = _outcome_key(selected, event)
        trades["event_ticker"] = event
        compiled = compile_event_trades_to_target(trades, selected.ticker, side)
        return GameOddsData(
            event_ticker=event,
            target_ticker=selected.ticker,
            target_side=side,
            target_label=selected.yes_sub_title or side,
            start_time=requested_start
            if requested_start is not None
            else trades["timestamp"].min(),
            start_time_source="provided"
            if requested_start is not None
            else "first_local_trade",
            markets=tuple(markets),
            trades=trades,
            compiled=compiled,
        )
    if source != "api":
        raise ValueError("source must be 'local' or 'api'")

    owns_client = client is None
    active_client: GameOddsClient = client if client is not None else KalshiClient()

    try:
        event_ticker, markets, input_market = discover_game_markets(
            ticker,
            active_client,
        )
        _validate_game_markets(event_ticker, markets)

        if requested_start is None:
            start = _infer_game_start_time(active_client, event_ticker)
            start_time_source = "kalshi_milestone"
        else:
            start = requested_start
            start_time_source = "provided"
        if end is not None and start > end:
            raise ValueError("start_time must be before or equal to end_time")

        target_market = _select_target_market(
            event_ticker=event_ticker,
            markets=markets,
            requested_target=target,
            input_market=input_market,
        )
        target_side = _outcome_key(target_market, event_ticker)
        target_label = target_market.yes_sub_title.strip() or target_side

        min_ts = int(start.timestamp()) if start is not None else None
        max_ts = int(end.timestamp()) if end is not None else None
        trades = _load_complete_trades(
            client=active_client,
            markets=markets if len(markets) == 2 else [target_market],
            event_ticker=event_ticker,
            min_ts=min_ts,
            max_ts=max_ts,
        )
        permit_waiting = allow_empty and end is None and any(
            market.status.casefold() != "settled" for market in markets
        )
        if trades.empty and not permit_waiting:
            raise ValueError(
                f"No trades were returned for the selected market(s) in {event_ticker!r}."
            )
        if trades.empty:
            trades = pd.DataFrame(columns=[
                "trade_id", "event_ticker", "ticker", "source_side", "timestamp",
                "yes_price", "no_price", "count", "taker_side",
            ])
            trades["timestamp"] = pd.Series(dtype="datetime64[ns, UTC]")

        if start is not None:
            trades = trades[trades["timestamp"] >= start]
        if end is not None:
            trades = trades[trades["timestamp"] <= end]
        trades = trades.reset_index(drop=True)
        if trades.empty and not permit_waiting:
            raise ValueError("No trades remain inside the requested time range.")

        compiled = (
            compile_event_trades_to_target(
                trades,
                target_ticker=target_market.ticker,
                target_side=target_side,
            )
            if not trades.empty
            else pd.DataFrame(columns=[
                "timestamp", "target_raw_prob", "taker_prob", "maker_prob", "count"
            ])
        )

        return GameOddsData(
            event_ticker=event_ticker,
            target_ticker=target_market.ticker,
            target_side=target_side,
            target_label=target_label,
            start_time=start,
            start_time_source=start_time_source,
            markets=tuple(markets),
            trades=trades,
            compiled=compiled,
        )
    finally:
        if owns_client:
            active_client.close()


def game_is_unsettled(game: GameOddsData) -> bool:
    """A game stays live until all of its outcome markets settle."""
    return any(market.status.casefold() != "settled" for market in game.markets)


def refresh_api_game(game: GameOddsData, client: GameOddsClient) -> bool:
    """Fetch recent trades and current market status without reloading history."""
    refreshed_markets = tuple(
        _find_market(client, market.ticker) or market for market in game.markets
    )
    previous_statuses = tuple(market.status for market in game.markets)

    latest = game.trades["timestamp"].max() if not game.trades.empty else None
    min_ts = (
        max(
            int(game.start_time.timestamp()),
            int(latest.timestamp()) - LIVE_TRADE_OVERLAP_SECONDS,
        )
        if latest is not None
        else int(game.start_time.timestamp())
    )
    trade_markets = (
        list(refreshed_markets)
        if len(refreshed_markets) == 2
        else [next(m for m in refreshed_markets if m.ticker == game.target_ticker)]
    )
    recent = _load_complete_trades(
        client=client,
        markets=trade_markets,
        event_ticker=game.event_ticker,
        min_ts=min_ts,
        max_ts=None,
    )
    game.markets = refreshed_markets
    if not recent.empty:
        new_trades = recent[~recent["trade_id"].isin(game.trades["trade_id"])]
        if not new_trades.empty:
            game.trades = (
                pd.concat([game.trades, new_trades], ignore_index=True)
                .sort_values(["timestamp", "trade_id"], kind="stable")
                .reset_index(drop=True)
            )
            game.compiled = compile_event_trades_to_target(
                game.trades, game.target_ticker, game.target_side
            )
            return True
    return previous_statuses != tuple(market.status for market in refreshed_markets)


def build_game_odds_figure(
    compiled: pd.DataFrame,
    *,
    event_ticker: str,
    target_label: str,
    resample_frequency: str | None = "1s",
    volatility_cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
    show_fee_lines: bool = True,
    width: int = 2500,
    height: int = 1300,
    sportsbook_odds: pd.DataFrame | None = None,
    pbp_events: pd.DataFrame | None = None,
) -> go.Figure:
    """Build a Plotly odds chart, adding optional overlays only when present."""
    if compiled.empty:
        raise ValueError("compiled trades cannot be empty")
    if width < 1 or height < 1:
        raise ValueError("width and height must be positive")

    plot_data = _resample_for_plot(compiled, resample_frequency)
    escaped_target = html.escape(str(target_label))
    escaped_event = html.escape(event_ticker)

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=plot_data["timestamp"],
            y=plot_data["target_raw_prob"],
            mode="lines",
            line_shape="hv" if resample_frequency is None else "linear",
            line={"color": "#1f4aff", "width": 4},
            name="Kalshi",
            hovertemplate="Time=%{x}<br>Kalshi=%{y:.2%}<extra></extra>",
        )
    )

    if show_fee_lines:
        figure.add_trace(
            go.Scatter(
                x=plot_data["timestamp"],
                y=plot_data["taker_prob"],
                mode="lines",
                line_shape="hv" if resample_frequency is None else "linear",
                line={"color": "#ff6b57", "width": 2, "dash": "dash"},
                opacity=0.9,
                name="Kalshi taker",
                hovertemplate="Time=%{x}<br>Taker=%{y:.2%}<extra></extra>",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=plot_data["timestamp"],
                y=plot_data["maker_prob"],
                mode="lines",
                line_shape="hv" if resample_frequency is None else "linear",
                line={"color": "#00b894", "width": 2, "dash": "dot"},
                opacity=0.9,
                name="Kalshi maker",
                hovertemplate="Time=%{x}<br>Maker=%{y:.2%}<extra></extra>",
            )
        )

    figure.update_layout(
        title={
            "text": (
                f"{escaped_target} Implied Probability — Kalshi"
                f"<br><sup>{escaped_event} | Kalshi Trade Timeline</sup>"
            ),
            "x": 0.04,
            "xanchor": "left",
            "font": {"size": 32},
        },
        template="plotly_white",
        width=width,
        height=height,
        hovermode="x unified",
        dragmode="pan",
        legend={
            "title": "Source",
            "orientation": "v",
            "x": 1.02,
            "y": 1.0,
            "bgcolor": "rgba(255,255,255,0.85)",
            "bordercolor": "rgba(0,0,0,0.12)",
            "borderwidth": 1,
            "font": {"size": 16},
            "title_font": {"size": 18},
        },
        margin={"l": 110, "r": 360, "t": 130, "b": 180},
        font={"size": 18},
        meta={
            "event_ticker": event_ticker,
            "target_label": target_label,
            "resample_frequency": resample_frequency,
            "rolling_volatility": rolling_volatility_payload(
                compiled, cadence_seconds=volatility_cadence_seconds
            ),
        },
    )
    figure.update_yaxes(
        title_text=f"{target_label} Probability",
        range=[0, 1],
        fixedrange=True,
        tickformat=".0%",
        gridcolor="rgba(0,0,0,0.08)",
        zeroline=False,
        title_font={"size": 20},
        tickfont={"size": 16},
        automargin=True,
    )
    figure.update_xaxes(
        title_text="Timestamp",
        tickformat="%H:%M:%S<br>%Y-%m-%d",
        showgrid=True,
        gridcolor="rgba(0,0,0,0.06)",
        title_font={"size": 20},
        tickfont={"size": 16},
        automargin=True,
    )

    from src.analysis.kalshi.game_overlays import add_game_overlays

    figure = add_game_overlays(figure, compiled, sportsbook_odds, pbp_events)
    figure = _add_volume_panel(figure, compiled, resample_frequency)
    return _constrain_time_axis(figure, compiled)


def figure_display_config(
    event_ticker: str,
    target_label: str,
    *,
    width: int = 2500,
    height: int = 1300,
) -> dict:
    """Return the browser toolbar/interaction settings used by the notebook."""
    filename = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        f"{event_ticker}_{target_label}_kalshi_chart",
    ).strip("_")
    return {
        "displayModeBar": True,
        "displaylogo": False,
        "scrollZoom": True,
        "responsive": True,
        "toImageButtonOptions": {
            "format": "png",
            "filename": filename,
            "height": height,
            "width": width,
            "scale": 2,
        },
    }


def volume_rebucket_script() -> str:
    """Browser handler that sums volume into buckets for the visible time span."""
    return Path(__file__).with_name("game_volume_browser.js").read_text(encoding="utf-8")


def visualize_game_odds(
    ticker: str,
    *,
    target: str | None = None,
    start_time: str | datetime | pd.Timestamp | None = None,
    end_time: str | datetime | pd.Timestamp | None = None,
    resample_frequency: str | None = "1s",
    show_fee_lines: bool = True,
    show: bool = True,
    renderer: str | None = "browser",
    width: int = 2500,
    height: int = 1300,
    client: GameOddsClient | None = None,
    source: str = "api",
    data_dir: str | Path = DEFAULT_DATA_DIR,
    sportsbook_path: str | Path | None = None,
    playbyplay_path: str | Path | None = None,
    sportsbook_team: str | None = None,
    sportsbook_side: str | None = None,
) -> go.Figure:
    """Fetch one game's Kalshi trades and optionally pop out its visualizer."""
    game = load_game_odds(
        ticker,
        target=target,
        start_time=start_time,
        end_time=end_time,
        client=client,
        source=source,
        data_dir=data_dir,
    )
    from src.analysis.kalshi.game_overlays import load_game_overlays

    books, events = load_game_overlays(
        game,
        data_dir=data_dir,
        sportsbook_path=sportsbook_path,
        playbyplay_path=playbyplay_path,
        sportsbook_team=sportsbook_team,
        sportsbook_side=sportsbook_side,
    )
    figure = build_game_odds_figure(
        game.compiled,
        event_ticker=game.event_ticker,
        target_label=game.target_label,
        resample_frequency=resample_frequency,
        show_fee_lines=show_fee_lines,
        width=width,
        height=height,
        sportsbook_odds=books,
        pbp_events=events,
    )

    if show:
        show_options = {
            "config": figure_display_config(
                game.event_ticker,
                game.target_label,
                width=width,
                height=height,
            )
        }
        if renderer is not None:
            show_options["renderer"] = renderer
        figure.show(**show_options)

    return figure


def _get_complete_event_markets(
    client: GameOddsClient,
    event_ticker: str,
) -> list[Market]:
    markets_by_ticker: dict[str, Market] = {}
    # Read live first so a moving historical cutoff can create overlap, not a gap.
    for historical in (False, True):
        try:
            markets = client.get_event_markets(
                event_ticker,
                historical=historical,
                limit=1000,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                continue
            raise
        for market in markets:
            markets_by_ticker.setdefault(market.ticker.upper(), market)

    return sorted(markets_by_ticker.values(), key=lambda market: market.ticker)


def _find_market(client: GameOddsClient, ticker: str) -> Market | None:
    for historical in (False, True):
        try:
            market = client.get_market(ticker, historical=historical)
            if market is not None:
                return market
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
    return None


def _infer_game_start_time(
    client: GameOddsClient,
    event_ticker: str,
) -> pd.Timestamp:
    try:
        milestones = client.get_event_milestones(event_ticker, limit=500)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        milestones = []

    normalized_event = event_ticker.upper()
    candidates: list[tuple[int, pd.Timestamp]] = []
    for milestone in milestones:
        primary_tickers = {
            str(ticker).upper() for ticker in milestone.get("primary_event_tickers", [])
        }
        related_tickers = {
            str(ticker).upper() for ticker in milestone.get("related_event_tickers", [])
        }
        linked_tickers = primary_tickers | related_tickers
        if linked_tickers and normalized_event not in linked_tickers:
            continue

        try:
            parsed_start = _to_utc_timestamp(
                milestone.get("start_date"),
                "milestone start_date",
            )
        except ValueError:
            continue
        if parsed_start is None:
            continue

        priority = 0 if normalized_event in primary_tickers else 1
        candidates.append((priority, parsed_start))

    if not candidates:
        raise ValueError(
            f"Kalshi did not return a scheduled game start for {event_ticker!r}. "
            "Pass start_time= or --start explicitly."
        )

    best_priority = min(priority for priority, _ in candidates)
    start_times = {start for priority, start in candidates if priority == best_priority}
    if len(start_times) != 1:
        choices = ", ".join(sorted(start.isoformat() for start in start_times))
        raise ValueError(
            f"Kalshi returned multiple possible game starts for {event_ticker!r}: "
            f"{choices}. Pass start_time= or --start explicitly."
        )

    return next(iter(start_times))


def _select_target_market(
    *,
    event_ticker: str,
    markets: list[Market],
    requested_target: str | None,
    input_market: Market | None,
) -> Market:
    if requested_target:
        wanted = requested_target.strip().casefold()
        matches = []
        for market in markets:
            identifiers = {
                market.ticker.casefold(),
                _outcome_key(market, event_ticker).casefold(),
                market.yes_sub_title.strip().casefold(),
            }
            identifiers.discard("")
            if wanted in identifiers:
                matches.append(market)

        if len(matches) == 1:
            return matches[0]
        choices = _target_choices(markets, event_ticker)
        if matches:
            raise ValueError(
                f"Target {requested_target!r} is ambiguous. Choose one of: {choices}."
            )
        raise ValueError(
            f"Target {requested_target!r} does not match this event. "
            f"Choose one of: {choices}."
        )

    if input_market is not None:
        for market in markets:
            if market.ticker.upper() == input_market.ticker.upper():
                return market
        raise ValueError(
            f"Input market {input_market.ticker!r} was not returned among the "
            f"markets for event {event_ticker!r}."
        )

    if len(markets) == 1:
        return markets[0]

    suffix_matches = [
        market
        for market in markets
        if event_ticker.upper().endswith(_outcome_key(market, event_ticker).upper())
    ]
    if len(suffix_matches) == 1:
        return suffix_matches[0]

    raise ValueError(
        "Could not infer the target outcome from the event ticker. "
        f"Pass target= with one of: {_target_choices(markets, event_ticker)}."
    )


def _target_choices(markets: list[Market], event_ticker: str) -> str:
    return ", ".join(
        f"{_outcome_key(market, event_ticker)} ({market.ticker})" for market in markets
    )


def _outcome_key(market: Market, event_ticker: str) -> str:
    prefix = f"{event_ticker.upper()}-"
    ticker = market.ticker.upper()
    if ticker.startswith(prefix):
        return ticker[len(prefix) :]
    return ticker.rsplit("-", 1)[-1]


def _load_complete_trades(
    *,
    client: GameOddsClient,
    markets: list[Market],
    event_ticker: str,
    min_ts: int | None,
    max_ts: int | None,
) -> pd.DataFrame:
    records: list[dict] = []

    for market in markets:
        trades_by_id: dict[str, dict] = {}
        # Live first avoids a gap if Kalshi's historical cutoff moves mid-fetch.
        for historical in (False, True):
            try:
                fetched = client.get_market_trades_data(
                    market.ticker,
                    limit=1000,
                    verbose=False,
                    min_ts=min_ts,
                    max_ts=max_ts,
                    historical=historical,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    continue
                raise
            for trade in fetched:
                trade_id = str(trade.get("trade_id") or "").strip()
                if not trade_id:
                    raise ValueError(
                        f"Kalshi returned a trade without trade_id for {market.ticker}."
                    )
                trades_by_id.setdefault(trade_id, trade)

        source_side = _outcome_key(market, event_ticker)
        for trade_id, trade in trades_by_id.items():
            yes_price = _trade_probability(trade, "yes")
            if (
                trade.get("no_price_dollars") is not None
                or trade.get("no_price") is not None
            ):
                no_price = _trade_probability(trade, "no")
            else:
                no_price = 1 - yes_price
            records.append(
                {
                    "trade_id": trade_id,
                    "event_ticker": event_ticker,
                    "ticker": market.ticker.upper(),
                    "source_side": source_side,
                    "timestamp": trade.get("created_time"),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "count": _trade_count(trade),
                    "taker_side": trade.get("taker_side", ""),
                }
            )

    if not records:
        return pd.DataFrame()

    frame = pd.DataFrame.from_records(records)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    return (
        frame.dropna(subset=["timestamp"])
        .drop_duplicates(subset=["trade_id"], keep="last")
        .sort_values(["timestamp", "trade_id"], kind="stable")
        .reset_index(drop=True)
    )


def _validate_game_markets(
    event_ticker: str,
    markets: list[Market],
) -> None:
    if not markets:
        raise ValueError(f"No outcome markets found for {event_ticker!r}.")

    wrong_event = [
        market.ticker
        for market in markets
        if market.event_ticker.upper() != event_ticker.upper()
    ]
    if wrong_event:
        raise ValueError(
            "Kalshi returned market(s) from a different event: "
            + ", ".join(wrong_event)
        )

    non_binary = [
        market.ticker
        for market in markets
        if market.market_type and market.market_type.casefold() != "binary"
    ]
    if non_binary:
        raise ValueError(
            "The single-game visualizer only supports binary markets: "
            + ", ".join(non_binary)
        )

    outcome_keys = [_outcome_key(market, event_ticker) for market in markets]
    if any(not outcome for outcome in outcome_keys) or len(set(outcome_keys)) != len(
        markets
    ):
        raise ValueError(
            f"Event {event_ticker!r} does not have distinct outcome suffixes."
        )


def _trade_probability(trade: dict[str, Any], side: str) -> float:
    dollars_key = f"{side}_price_dollars"
    cents_key = f"{side}_price"

    try:
        if trade.get(dollars_key) is not None:
            probability = Decimal(str(trade[dollars_key]))
        elif trade.get(cents_key) is not None:
            probability = Decimal(str(trade[cents_key])) / Decimal(100)
        else:
            raise ValueError(f"trade is missing {dollars_key} and {cents_key}")
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"trade has an invalid {side.upper()} price") from exc

    if not Decimal(0) <= probability <= Decimal(1):
        raise ValueError(
            f"trade has {side.upper()} probability outside [0, 1]: {probability}"
        )
    return float(probability)


def _trade_count(trade: dict[str, Any]) -> float | int | None:
    raw_count = trade.get("count_fp")
    if raw_count is None:
        raw_count = trade.get("count")
    if raw_count is None:
        return None
    try:
        count = Decimal(str(raw_count))
    except (InvalidOperation, TypeError):
        return None
    return int(count) if count == count.to_integral_value() else float(count)


def _add_volume_panel(
    figure: go.Figure,
    compiled: pd.DataFrame,
    frequency: str | None,
) -> go.Figure:
    """Show traded contracts per interval below the odds and event tracks."""
    if "count" not in compiled:
        return figure
    volume = compiled[["timestamp", "count"]].copy()
    volume["timestamp"] = pd.to_datetime(volume["timestamp"], utc=True, errors="coerce")
    volume["count"] = pd.to_numeric(volume["count"], errors="coerce")
    volume = volume.dropna(subset=["timestamp", "count"])
    volume = volume[volume["count"] >= 0]
    if volume.empty:
        return figure
    volume = volume.set_index("timestamp")["count"].groupby(level=0).sum()
    minimum_ms = (
        max(1, math.ceil(pd.to_timedelta(frequency).total_seconds() * 1000))
        if frequency
        else 1
    )
    span_ms = (volume.index.max() - volume.index.min()).total_seconds() * 1000
    bucket_ms = _volume_bucket_ms(span_ms, minimum_ms)
    bucketed = _bucket_volume(volume, bucket_ms)

    has_events = "yaxis2" in figure.layout
    axis_number = 3 if has_events else 2
    xaxis = f"x{axis_number}"
    yaxis = f"y{axis_number}"
    xlayout = f"xaxis{axis_number}"
    ylayout = f"yaxis{axis_number}"
    figure.add_trace(
        go.Bar(
            x=bucketed.index + pd.to_timedelta(bucket_ms / 2, unit="ms"),
            y=bucketed.to_numpy(),
            width=bucket_ms,
            xaxis=xaxis,
            yaxis=yaxis,
            name="Volume",
            marker_color="#8294ba",
            hovertemplate="Bucket center=%{x}<br>Contracts=%{y:,.0f}<extra></extra>",
            meta={
                "timestamps": [timestamp.isoformat() for timestamp in volume.index],
                "counts": volume.tolist(),
                "minimum_ms": minimum_ms,
                "bucket_ms": bucket_ms,
            },
        )
    )
    if has_events:
        figure.update_layout(
            yaxis=dict(domain=[0.36, 1]),
            yaxis2=dict(domain=[0.21, 0.30]),
            xaxis2=dict(showticklabels=False, title=None),
        )
    else:
        figure.update_layout(yaxis=dict(domain=[0.26, 1]))
    figure.update_layout(
        xaxis=dict(showticklabels=False, title=None),
        **{
            xlayout: dict(
                anchor=yaxis,
                matches="x",
                title="Timestamp (UTC)",
                tickformat="%H:%M:%S<br>%Y-%m-%d",
            ),
            ylayout: dict(
                domain=[0, 0.14 if has_events else 0.17],
                anchor=xaxis,
                title="Volume (contracts)",
                fixedrange=True,
                rangemode="tozero",
                gridcolor="rgba(0,0,0,0.08)",
                zeroline=False,
            ),
        },
    )
    return figure


def _volume_bucket_ms(span_ms: float, minimum_ms: int) -> int:
    """Choose a readable interval with roughly 120 bars across the view."""
    desired = max(minimum_ms, span_ms / VOLUME_TARGET_BARS)
    magnitude = 10 ** math.floor(math.log10(desired))
    for multiplier in (1, 2, 5, 10):
        candidate = multiplier * magnitude
        if candidate >= desired:
            return int(candidate)
    raise AssertionError("unreachable volume bucket size")


def _constrain_time_axis(figure: go.Figure, compiled: pd.DataFrame) -> go.Figure:
    """Start near the trades, with a firm left edge and breathing room on the right."""
    timestamps = pd.to_datetime(compiled["timestamp"], utc=True, errors="coerce").dropna()
    if timestamps.empty:
        return figure
    span = timestamps.max() - timestamps.min()
    padding = max(span * 0.02, pd.Timedelta(seconds=1))
    if span == pd.Timedelta(0):
        padding = pd.Timedelta(seconds=30)
    lower = (timestamps.min() - padding).isoformat()
    upper = (timestamps.max() + padding).isoformat()
    soft_upper = (
        timestamps.max() + padding + max(span * 0.60, pd.Timedelta(seconds=30))
    ).isoformat()
    figure.update_xaxes(range=[lower, upper], minallowed=lower, maxallowed=soft_upper)
    figure.update_layout(
        meta={
            **dict(figure.layout.meta or {}),
            "time_bounds": {"left": lower, "right": upper},
        }
    )
    return figure


def _bucket_volume(volume: pd.Series, bucket_ms: int) -> pd.Series:
    bins = (volume.index.as_unit("ms").asi8 // bucket_ms) * bucket_ms
    return volume.groupby(pd.to_datetime(bins, unit="ms", utc=True)).sum()


def _resample_for_plot(
    compiled: pd.DataFrame,
    frequency: str | None,
) -> pd.DataFrame:
    plot_data = compiled.copy()
    plot_data["timestamp"] = pd.to_datetime(
        plot_data["timestamp"], utc=True, errors="coerce"
    )
    plot_data = plot_data.dropna(subset=["timestamp"]).sort_values(
        "timestamp", kind="stable"
    )
    if plot_data.empty:
        raise ValueError("compiled trades do not contain any valid timestamps")

    if frequency is None:
        return plot_data

    try:
        interval = pd.to_timedelta(frequency)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid resample frequency {frequency!r}.") from exc
    if interval <= pd.Timedelta(0):
        raise ValueError("resample_frequency must be positive")

    duration = plot_data["timestamp"].max() - plot_data["timestamp"].min()
    estimated_points = int(duration / interval) + 1
    if estimated_points > MAX_RESAMPLED_POINTS:
        raise ValueError(
            f"Resampling at {frequency!r} would create about "
            f"{estimated_points:,} points. Use a larger interval, pass "
            "resample_frequency=None, or narrow start_time/end_time."
        )

    value_columns = ["target_raw_prob", "taker_prob", "maker_prob"]
    return (
        plot_data.set_index("timestamp")[value_columns]
        .resample(frequency)
        .last()
        .ffill()
        .reset_index()
    )


def _to_utc_timestamp(
    value: str | datetime | pd.Timestamp | None,
    argument_name: str,
) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value, utc=True, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{argument_name} must be a valid date/time value") from exc
    if not isinstance(parsed, pd.Timestamp) or pd.isna(parsed):
        raise ValueError(f"{argument_name} must be a single date/time value")
    return parsed
