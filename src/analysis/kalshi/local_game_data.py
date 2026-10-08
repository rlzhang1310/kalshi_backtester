"""Read game metadata and selected trades from the repository's Parquet data."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import duckdb
import pandas as pd

from src.analysis.kalshi.util.sports_markets import (
    classify_market_category,
    classify_sport,
    market_category_case_sql,
    normalize_sport,
)
from src.indexers.kalshi.models import Market
from src.analysis.kalshi.util.trades import family_trade_files


DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data"
FILE_BATCH_SIZE = 512


def _files(directory: Path) -> list[str]:
    # macOS resource-fork files (._*.parquet) are not Parquet files.
    return sorted(
        str(p) for p in directory.glob("*.parquet") if not p.name.startswith("._")
    )


class LocalGameStore:
    """Bound file metadata work and push ticker filters into each Parquet scan."""

    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir).expanduser()
        self.market_files = _files(self.data_dir / "kalshi" / "markets")
        if not self.market_files:
            raise ValueError(
                f"No market Parquet files in {self.data_dir / 'kalshi' / 'markets'}"
            )

    def _query(
        self, files: list[str], columns: str, where: str = "true", params=()
    ) -> pd.DataFrame:
        frames = []
        with duckdb.connect() as con:
            con.execute("SET enable_progress_bar=false")
            for offset in range(0, len(files), FILE_BATCH_SIZE):
                frame = con.execute(
                    f"SELECT {columns} FROM read_parquet(?, union_by_name=true) WHERE {where}",
                    [files[offset : offset + FILE_BATCH_SIZE], *params],
                ).fetchdf()
                if not frame.empty:
                    frames.append(frame)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def list_games(self, *, sport: str | None = None, search: str = "") -> pd.DataFrame:
        """List game-winner events; titles/outcomes are searchable without trade scans.

        Close time is deliberately not presented as game start: the stored
        market schema has no scheduled kickoff/first-pitch timestamp.
        """
        where = f"({market_category_case_sql('event_ticker')}) = 'moneyline'"
        frame = self._query(
            self.market_files,
            "DISTINCT ticker, event_ticker, title, yes_sub_title, no_sub_title",
            where,
        )
        columns = ["event_ticker", "sport", "title", "outcomes", "market_count"]
        if frame.empty:
            return pd.DataFrame(columns=columns)
        frame = frame.drop_duplicates("ticker").fillna("")
        # Classify unique families first, rather than every market in a large archive.
        families = frame["event_ticker"].str.split("-").str[0]
        sports = {family: classify_sport(family) for family in families.unique()}
        categories = {
            family: classify_market_category(family) for family in families.unique()
        }
        frame["sport"] = families.map(sports)
        frame = frame[frame["sport"].notna() & families.map(categories).eq("moneyline")]
        if sport:
            frame = frame[frame["sport"].eq(normalize_sport(sport))]
        games = (
            frame.groupby("event_ticker", sort=True)
            .agg(
                sport=("sport", "first"),
                title=("title", "first"),
                outcomes=(
                    "yes_sub_title",
                    lambda values: " / ".join(dict.fromkeys(v for v in values if v)),
                ),
                market_count=("ticker", "nunique"),
            )
            .reset_index()
        )
        if search:
            text = (
                games["event_ticker"] + " " + games["title"] + " " + games["outcomes"]
            )
            games = games[text.str.contains(search, case=False, regex=False)]
        return games[columns].reset_index(drop=True)

    def resolve_markets(self, ticker: str) -> tuple[str, list[Market], Market | None]:
        ticker = ticker.strip().upper()
        frame = self._query(
            self.market_files, "*", "event_ticker = ? OR ticker = ?", (ticker, ticker)
        )
        if frame.empty:
            raise ValueError(
                f"No local game or market {ticker!r}. Use --source api to fetch it from Kalshi."
            )
        event = str(frame.iloc[0]["event_ticker"])
        if event != ticker:
            frame = self._query(self.market_files, "*", "event_ticker = ?", (event,))
        markets = []
        for record in frame.drop_duplicates("ticker", keep="last").to_dict("records"):
            values = {field.name: record.get(field.name) for field in fields(Market)}
            for name, value in values.items():
                if pd.isna(value):
                    values[name] = None
            for name in ("yes_sub_title", "no_sub_title", "title", "market_type"):
                values[name] = values[name] or ""
            markets.append(Market(**values))
        markets.sort(key=lambda market: market.ticker)
        return event, markets, next((m for m in markets if m.ticker == ticker), None)

    def load_trades(
        self,
        tickers: list[str],
        *,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
    ) -> pd.DataFrame:
        dataset = self.data_dir / "kalshi" / "trades_by_series"
        families = {ticker.split("-", 1)[0] for ticker in tickers}
        files = [str(path) for family in sorted(families)
                 for path in family_trade_files(dataset, family)]
        if not files:
            raise ValueError(
                "No trades in the published family partition for this game. Use --source api."
            )
        where = "ticker IN (" + ",".join("?" for _ in tickers) + ")"
        params: list = list(tickers)
        if start is not None:
            where += " AND created_time >= ?"
            params.append(start.to_pydatetime())
        if end is not None:
            where += " AND created_time <= ?"
            params.append(end.to_pydatetime())
        frame = self._query(
            files,
            "trade_id, ticker, count, yes_price, no_price, taker_side, created_time",
            where,
            params,
        )
        if frame.empty:
            raise ValueError(
                "No local trades for this game in the requested range. Use --source api to fetch it."
            )
        # Match the backtester's authoritative taker-side price repair. Stored
        # prices are always cents, including values 0 and 1.
        side = frame["taker_side"].str.lower()
        price = pd.to_numeric(frame["yes_price"], errors="coerce").where(
            side.eq("yes"), pd.to_numeric(frame["no_price"], errors="coerce")
        )
        valid = side.isin(["yes", "no"]) & price.between(0, 100)
        frame["yes_price"] = price.where(side.eq("yes"), 100 - price) / 100
        frame["no_price"] = 1 - frame["yes_price"]
        frame["timestamp"] = pd.to_datetime(
            frame["created_time"], utc=True, errors="coerce"
        )
        frame = frame[valid].dropna(subset=["timestamp"])
        if frame.empty:
            raise ValueError("No valid local trades remain for this game.")
        return (
            frame.drop_duplicates("trade_id")
            .sort_values(["timestamp", "trade_id"])
            .reset_index(drop=True)
        )


def list_local_games(
    data_dir: str | Path = DEFAULT_DATA_DIR, **filters
) -> pd.DataFrame:
    """Public catalog entry point for notebooks and the terminal picker."""
    return LocalGameStore(data_dir).list_games(**filters)
