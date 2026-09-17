"""Optional sportsbook and play-by-play adapters for the imported notebook."""

from __future__ import annotations

import html
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import plotly.graph_objects as go

if TYPE_CHECKING:
    from src.analysis.kalshi.single_game_odds_time_series import GameOddsData


EVENT_COLUMNS = [
    "timestamp",
    "event_type",
    "event_label",
    "event_detail",
    "score_text",
    "game_clock_text",
    "down_distance_text",
]
BOOK_COLUMNS = ["timestamp", "book", "target_raw_prob"]


def _timestamps(values: pd.Series, name: str) -> pd.Series:
    result = pd.to_datetime(values, utc=True, errors="coerce", format="mixed")
    if result.isna().any():
        raise ValueError(
            f"{name} contains missing or invalid timestamps; use ISO timestamps with a UTC offset."
        )
    return result.dt.as_unit("ns")


def _read_optional(path: str | Path | None, default: Path) -> pd.DataFrame | None:
    selected = Path(path) if path is not None else default
    if not selected.exists():
        if path is not None:
            raise ValueError(f"Overlay file does not exist: {selected}")
        return None
    if selected.suffix.lower() == ".parquet":
        return pd.read_parquet(selected)
    try:
        return pd.read_csv(selected)
    except pd.errors.EmptyDataError:
        return None


def prepare_sportsbook_odds(
    frame: pd.DataFrame | None,
    *,
    target: str,
    aliases: tuple[str, ...] = (),
    side: str | None = None,
) -> pd.DataFrame:
    """Use the selected team's direct odds, never invert its opponent's vig.

    Accept long-form timestamp/book/target_team plus implied_prob_raw or
    raw_odds (American), or SportsDataIO Updated/Sportsbook/HomeMoneyLine/
    AwayMoneyLine with an explicit home/away side mapping.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=BOOK_COLUMNS)
    data = frame.copy()
    if "Sportsbook" in data.columns:
        if side not in ("home", "away"):
            raise ValueError(
                "SportsDataIO odds require sportsbook_side='home'/'away' or --sportsbook-side."
            )
        price_column = "HomeMoneyLine" if side == "home" else "AwayMoneyLine"
        required = {"Updated", price_column}
        if not required.issubset(data.columns):
            raise ValueError(
                f"SportsDataIO odds require columns: {', '.join(sorted(required))}"
            )
        data = data.rename(
            columns={
                "Sportsbook": "book",
                "Updated": "timestamp",
                price_column: "raw_odds",
            }
        )
        data["target_team"] = target
    else:
        if "timestamp" not in data and "book_last_update" in data:
            data["timestamp"] = data["book_last_update"]
        if "book" not in data and "book_key" in data:
            data["book"] = data["book_key"]
    required = {"timestamp", "book", "target_team"}
    if not required.issubset(data.columns):
        raise ValueError(
            "Sportsbook odds require timestamp, book, target_team, and implied_prob_raw or raw_odds."
        )
    wanted = {value.strip().casefold() for value in (target, *aliases)}
    data = data[
        data["target_team"].astype(str).str.strip().str.casefold().isin(wanted)
    ].copy()
    if data.empty:
        raise ValueError(
            f"No sportsbook rows match {target!r}. Set --sportsbook-team to its name in the file."
        )
    if "implied_prob_raw" in data:
        probability = pd.to_numeric(data["implied_prob_raw"], errors="coerce")
        if (data["implied_prob_raw"].notna() & probability.isna()).any():
            raise ValueError("Sportsbook probabilities must be numeric.")
    elif "raw_odds" in data:
        odds = pd.to_numeric(data["raw_odds"], errors="coerce")
        if (data["raw_odds"].notna() & odds.isna()).any():
            raise ValueError("American sportsbook odds must be numeric.")
        # Missing moneylines are common; an explicit zero is not valid American odds.
        if odds.eq(0).any():
            raise ValueError("American sportsbook odds cannot be zero.")
        probability = (100 / (odds.abs() + 100)).where(
            odds >= 0, odds.abs() / (odds.abs() + 100)
        )
    else:
        raise ValueError(
            "Sportsbook odds require implied_prob_raw or raw_odds (American odds)."
        )
    if (probability.notna() & ~probability.between(0, 1)).any():
        raise ValueError("Sportsbook probabilities must be between 0 and 1.")
    data["target_raw_prob"] = probability
    data = data.dropna(subset=["target_raw_prob", "book"])
    data["timestamp"] = _timestamps(data["timestamp"], "Sportsbook odds")
    return (
        data[BOOK_COLUMNS]
        .sort_values("timestamp", kind="stable")
        .drop_duplicates(["timestamp", "book"], keep="last")
        .reset_index(drop=True)
    )


def prepare_pbp_events(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Accept canonical events or the reference notebook's SportsDataIO NFL CSV.

    Canonical input requires timestamp; all event metadata is optional. Raw
    input requires Created, with Description/IsScoringPlay and clock/score
    fields used when available. No provider or network dependency is needed.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    if "timestamp" in frame:
        events = frame.copy()
    elif "Created" in frame:
        rows = []
        for raw in frame.fillna("").to_dict("records"):
            detail = str(raw.get("Description", ""))
            quarter = str(raw.get("QuarterName", ""))
            minutes = raw.get("TimeRemainingMinutes", "")
            seconds = raw.get("TimeRemainingSeconds", "")
            clock = quarter
            if minutes != "" and seconds != "":
                clock = (
                    f"{quarter} {int(float(minutes))}:{int(float(seconds)):02d}".strip()
                )
            away = str(raw.get("AwayScore", "")).removesuffix(".0")
            home = str(raw.get("HomeScore", "")).removesuffix(".0")
            score = (
                f"{raw.get('Score.AwayTeam', 'Away')} {away} - {raw.get('Score.HomeTeam', 'Home')} {home}"
                if away or home
                else ""
            )
            down = str(raw.get("Down", "")).removesuffix(".0")
            distance = str(raw.get("Distance", "")).removesuffix(".0")
            base = dict(
                timestamp=raw["Created"],
                event_type="All plays",
                event_label=raw.get("Type") or "Play",
                event_detail=detail,
                score_text=score,
                game_clock_text=clock,
                down_distance_text=f"Down {down}, distance {distance}" if down else "",
            )
            rows.append(base)
            if str(raw.get("IsScoringPlay", "")).lower() in ("true", "1", "1.0"):
                rows.append(
                    {
                        **base,
                        "event_type": "Scoring play",
                        "event_label": "Score",
                        "event_detail": raw.get("ScoringPlay.PlayDescription")
                        or detail,
                    }
                )
            upper = detail.upper()
            if "END OF" in upper or "END QUARTER" in upper:
                rows.append(
                    {
                        **base,
                        "event_type": "Quarter end",
                        "event_label": f"{quarter} end".strip(),
                    }
                )
            if "TWO-MINUTE WARNING" in upper or "2-MINUTE WARNING" in upper:
                rows.append(
                    {
                        **base,
                        "event_type": "2-minute warning",
                        "event_label": f"{quarter} 2:00".strip(),
                    }
                )
        events = pd.DataFrame(rows)
    else:
        raise ValueError(
            "Play-by-play requires timestamp (canonical events) or Created (SportsDataIO)."
        )
    events["timestamp"] = _timestamps(events["timestamp"], "Play-by-play")
    for column in EVENT_COLUMNS[1:]:
        default = "All plays" if column == "event_type" else ""
        if column not in events:
            events[column] = default
        events[column] = events[column].fillna(default).astype(str)
    return (
        events[EVENT_COLUMNS]
        .sort_values("timestamp", kind="stable")
        .reset_index(drop=True)
    )


def load_game_overlays(
    game: GameOddsData,
    *,
    data_dir: str | Path,
    sportsbook_path: str | Path | None = None,
    playbyplay_path: str | Path | None = None,
    sportsbook_team: str | None = None,
    sportsbook_side: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = Path(data_dir)
    books = _read_optional(
        sportsbook_path, root / "sportsbook" / f"{game.event_ticker}.csv"
    )
    plays = _read_optional(
        playbyplay_path, root / "playbyplay" / f"{game.event_ticker}.csv"
    )
    # A supplied combined file can hold many games when it includes this key.
    for frame in (books, plays):
        if frame is not None and "event_ticker" in frame:
            frame.drop(
                frame.index[
                    ~frame["event_ticker"].astype(str).str.upper().eq(game.event_ticker)
                ],
                inplace=True,
            )
    return (
        prepare_sportsbook_odds(
            books,
            target=sportsbook_team or game.target_side,
            aliases=() if sportsbook_team else (game.target_label, game.target_ticker),
            side=sportsbook_side,
        ),
        prepare_pbp_events(plays),
    )


def add_game_overlays(
    figure: go.Figure,
    compiled: pd.DataFrame,
    sportsbook_odds: pd.DataFrame | None,
    pbp_events: pd.DataFrame | None,
) -> go.Figure:
    """Add optional book curves and an aligned event track only when data exists."""
    timestamps = _timestamps(compiled["timestamp"], "Compiled trades")
    lower, upper = timestamps.min(), timestamps.max()
    if sportsbook_odds is not None and not sportsbook_odds.empty:
        books = sportsbook_odds.copy()
        books["timestamp"] = _timestamps(books["timestamp"], "Sportsbook odds")
        palette = ["#8e44ad", "#e67e22", "#16a085", "#e84393", "#6c5ce7", "#2d3436"]
        for index, (book, data) in enumerate(books.groupby("book", sort=True)):
            data = data.sort_values("timestamp")
            # Carry the last known pre-window quote forward to the window start.
            seed = data[data["timestamp"] < lower].tail(1).copy()
            seed["timestamp"] = lower
            data = pd.concat([seed, data[data["timestamp"].between(lower, upper)]])
            if data.empty:
                continue
            end_quote = data.tail(1).copy()
            end_quote["timestamp"] = upper
            data = pd.concat([data, end_quote]).drop_duplicates(
                "timestamp", keep="last"
            )
            figure.add_trace(
                go.Scatter(
                    x=data["timestamp"],
                    y=data["target_raw_prob"],
                    mode="lines",
                    name=html.escape(str(book)),
                    line=dict(shape="hv", width=2, color=palette[index % len(palette)]),
                    hovertemplate="Time=%{x}<br>Sportsbook=%{y:.2%}<extra>%{fullData.name}</extra>",
                )
            )
    events = prepare_pbp_events(pbp_events)
    events = events[events["timestamp"].between(lower, upper)]
    if events.empty:
        return figure
    figure.update_layout(
        yaxis=dict(domain=[0.24, 1]),
        yaxis2=dict(
            domain=[0, 0.14],
            anchor="x2",
            title="Game events",
            fixedrange=True,
            tickmode="array",
            tickvals=[0, 1, 2],
            ticktext=["Plays", "Period", "Scoring"],
            range=[-0.5, 2.5],
        ),
        xaxis=dict(showticklabels=False, title=None),
        xaxis2=dict(
            anchor="y2",
            matches="x",
            title="Timestamp (UTC)",
            tickformat="%H:%M:%S<br>%Y-%m-%d",
        ),
    )
    quote_data = compiled[["timestamp", "target_raw_prob"]].copy()
    quote_data["timestamp"] = timestamps
    quote_data = quote_data.sort_values("timestamp", kind="stable")
    for event_type, data in events.groupby("event_type", sort=False):
        scoring = event_type == "Scoring play"
        period = event_type in ("Quarter end", "2-minute warning", "Period end")
        track = 2 if scoring else 1 if period else 0
        color = "#d6a400" if scoring else "#a55eea" if period else "#888888"
        symbol = "star" if scoring else "diamond" if period else "circle"
        custom = data[EVENT_COLUMNS[2:]].map(lambda value: html.escape(str(value)))
        hover = "Time=%{x}<br>%{customdata[0]}<br>%{customdata[1]}<br>%{customdata[2]}<br>%{customdata[3]}<br>%{customdata[4]}<extra></extra>"
        figure.add_trace(
            go.Scatter(
                x=data["timestamp"],
                y=[track] * len(data),
                xaxis="x2",
                yaxis="y2",
                mode="markers",
                name=html.escape(event_type),
                customdata=custom,
                marker=dict(symbol=symbol, color=color, size=11 if scoring else 7),
                hovertemplate=hover,
            )
        )
        if scoring:
            aligned = pd.merge_asof(
                data, quote_data, on="timestamp", direction="backward"
            )
            figure.add_trace(
                go.Scatter(
                    x=aligned["timestamp"],
                    y=aligned["target_raw_prob"],
                    mode="markers",
                    name="Scoring play on odds",
                    showlegend=False,
                    customdata=custom,
                    marker=dict(symbol="star", color=color, size=13),
                    hovertemplate=hover,
                )
            )
        if period:
            for timestamp in data["timestamp"]:
                figure.add_shape(
                    type="line",
                    x0=timestamp,
                    x1=timestamp,
                    y0=0,
                    y1=1,
                    xref="x",
                    yref="y domain",
                    line=dict(color=color, width=1, dash="dot"),
                )
    return figure
