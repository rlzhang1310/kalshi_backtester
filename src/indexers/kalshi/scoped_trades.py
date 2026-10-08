"""Incremental trades for one Kalshi series or exact market in a published family dataset.

The copy-only migration is the immutable baseline. This module owns only
``scoped_*.parquet`` files and one checkpoint beside that baseline. No live
exchange orders, global trade pages, or archive rewrites are involved.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from uuid import uuid4

import duckdb
import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from src.analysis.kalshi.trade_reorganization import (
    DEFAULT_DESTINATION, DEFAULT_MARKETS, _check_space, _lock, _schema_json,
)
from src.indexers.kalshi.client import KalshiClient
from src.indexers.kalshi.global_trades import BATCH_SIZE, PAGE_LIMIT
from src.indexers.kalshi.global_trades_refresh import OVERLAP, SOURCES
from src.indexers.kalshi.models import Trade


FAMILY = re.compile(r"^[A-Z0-9]{2,64}$")
MARKET = re.compile(r"^[A-Z0-9]{2,64}(?:-[A-Z0-9_]+)+$")
WINDOW_SECONDS = 7 * 86400
CURSOR_RECHECK_SECONDS = 300
SCHEMA_COLUMNS = {"trade_id", "ticker", "count", "yes_price", "no_price",
                  "taker_side", "created_time", "_fetched_at", "series_ticker"}
STATE_NAME = "scoped_checkpoint.json"


class ExpiredCursor(RuntimeError):
    """A server rejected a cursor; replay the bounded window."""


def _fault(_stage):
    """Test seam at filesystem/checkpoint crash boundaries."""


def parse_time(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use an ISO-8601 UTC time, e.g. 2026-09-01T00:00:00Z") from exc
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0 or parsed.microsecond:
        raise argparse.ArgumentTypeError("Time must be UTC with whole-second precision")
    return int(parsed.timestamp())


def validate_selection(family: str | None, ticker: str | None) -> tuple[str, str]:
    if (family is None) == (ticker is None):
        raise ValueError("Select exactly one --family or --ticker")
    if family is not None:
        family = family.strip().upper()
        if not FAMILY.fullmatch(family):
            raise ValueError("--family must be one full series ticker, such as KXNFLGAME")
        return "family", family
    ticker = ticker.strip().upper()
    if not MARKET.fullmatch(ticker):
        raise ValueError("--ticker must be one full market ticker with a series and suffix")
    return "ticker", ticker


def _save_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _family_for_market(ticker: str, event_ticker: str, family: str) -> bool:
    return ticker.startswith(family + "-") and isinstance(event_ticker, str) and (
        event_ticker == family or event_ticker.startswith(family + "-")
    ) and ticker.startswith(event_ticker + "-")


def _coverage_add(coverage: list[list[int]], start: int, end: int) -> list[list[int]]:
    ranges = sorted([*coverage, [start, end]])
    merged: list[list[int]] = []
    for first, last in ranges:
        if merged and first <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return merged


def _epoch_ns(value: datetime) -> int:
    utc = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000


def _identity(row: tuple) -> str:
    return json.dumps(row[1:], separators=(",", ":"), ensure_ascii=False)


def _normalized(raw: dict, ticker: str, family: str, start: int, end: int, schema: pa.Schema):
    if not isinstance(raw, dict) or raw.get("ticker") != ticker:
        raise ValueError(f"Trade response contained a ticker other than {ticker}")
    if not isinstance(raw.get("trade_id"), str) or not raw["trade_id"].strip():
        raise ValueError("Trade without a usable trade_id")
    try:
        count = Decimal(str(raw["count_fp"]))
        yes = Decimal(str(raw["yes_price_dollars"])) * 100
        no = Decimal(str(raw["no_price_dollars"])) * 100
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError("Trade has missing or invalid fixed-point fields") from exc
    if not all(value.is_finite() for value in (count, yes, no)) or count <= 0 or any(
        value != value.to_integral_value() for value in (count, yes, no)
    ) or not 0 <= yes <= 100 or not 0 <= no <= 100:
        raise ValueError("Trade uses fractional contracts or sub-cent prices unsupported by the published schema")
    trade = Trade.from_dict(raw)
    if trade.created_time.tzinfo is None:
        raise ValueError("Trade timestamp has no UTC offset")
    if (trade.count, trade.yes_price, trade.no_price) != (int(count), int(yes), int(no)):
        raise ValueError("Legacy trade normalization would lose fixed-point precision")
    instant = _epoch_ns(trade.created_time)
    if instant < (start - 1) * 1_000_000_000 or instant >= (end + 1) * 1_000_000_000:
        raise ValueError("Trade outside the bounded API request")
    if not start * 1_000_000_000 <= instant < end * 1_000_000_000:
        return None  # Inclusive/exclusive API boundary behavior is not guaranteed.
    row = {**asdict(trade), "_fetched_at": datetime.now(timezone.utc)}
    if "series_ticker" in schema.names:
        row["series_ticker"] = family
    key = (trade.trade_id, ticker, trade.count, trade.yes_price, trade.no_price,
           trade.taker_side, instant)
    return row, key


class ScopedTrades:
    def __init__(self, client, *, destination: Path = DEFAULT_DESTINATION,
                 markets_dir: Path = DEFAULT_MARKETS, min_free_gib: float = 25,
                 now=None):
        self.client = client
        self.destination = Path(destination)
        self.markets_dir = Path(markets_dir)
        self.min_free_gib = min_free_gib
        self.now = now or (lambda: int(datetime.now(timezone.utc).timestamp()))
        self.state_path = self.destination / "_metadata" / STATE_NAME
        self.state: dict = {}
        self.dataset: dict = {}
        self.schema: pa.Schema | None = None
        self.stats = {"fetched": 0, "new": 0, "duplicate": 0, "recovered_files": 0,
                      "discovered": 0, "new_markets": 0, "failed": []}

    def _published(self):
        path = self.destination / "_metadata" / "dataset.json"
        if not path.is_file():
            raise RuntimeError("Published family dataset is required. Finish and verify the existing "
                               "reorganize_trades.py migration first; its running candidate is not a baseline.")
        dataset = json.loads(path.read_text(encoding="utf8"))
        if dataset.get("validation_status") != "passed" or dataset.get("partition_key") not in {
            "series_ticker", "ticker_family"
        } or not dataset.get("run_id"):
            raise RuntimeError("Family dataset metadata is not a verified published migration")
        catalog = self.destination / "_metadata" / "family_catalog.parquet"
        if not catalog.is_file():
            raise RuntimeError("Published family catalog is missing")
        folders = sorted(self.destination.glob(f"{dataset['partition_key']}=*"))
        files = (path for folder in folders for path in folder.glob("part-*.parquet"))
        first = next(files, None)
        if first is None:
            raise RuntimeError("Published dataset has no base Parquet files")
        schema = pq.read_schema(first).remove_metadata()
        if _schema_json(schema) != dataset.get("schema") or not set(schema.names) <= SCHEMA_COLUMNS:
            raise RuntimeError("Published trade schema is not supported by the scoped collector")
        self.dataset, self.schema = dataset, schema

    def _load_state(self):
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding="utf8"))
            if state.get("version") != 1 or state.get("base_run_id") != self.dataset["run_id"] or state.get(
                "partition_key"
            ) != self.dataset["partition_key"]:
                raise RuntimeError("Scoped checkpoint belongs to a different published baseline")
        else:
            state = {"version": 1, "base_run_id": self.dataset["run_id"],
                     "partition_key": self.dataset["partition_key"], "families": {},
                     "markets": {}, "files": {}}
        self.state = state

    def _save(self):
        _save_json(self.state_path, self.state)

    def _partition(self, family: str) -> Path:
        return self.destination / f"{self.dataset['partition_key']}={family}"

    def _reconcile_files(self, family: str):
        known = self.state["files"]
        for relative in list(known):
            if not relative.startswith(f"{self.dataset['partition_key']}={family}/"):
                continue
            if not (self.destination / relative).is_file():
                raise RuntimeError(f"Committed scoped batch is missing: {relative}")
        for file in self._partition(family).glob("scoped_*.parquet"):
            relative = file.relative_to(self.destination).as_posix()
            if relative in known:
                continue
            if _schema_json(pq.read_schema(file).remove_metadata()) != self.dataset["schema"]:
                raise RuntimeError(f"Uncheckpointed scoped file has incompatible schema: {file}")
            table = pq.read_table(file, columns=["ticker", "trade_id"])
            family = file.parent.name.split("=", 1)[1]
            if any(not isinstance(ticker, str) or not ticker.startswith(family + "-")
                   for ticker in table["ticker"].to_pylist()):
                raise RuntimeError(f"Uncheckpointed scoped file has an invalid family: {file}")
            if any(not item for item in table["trade_id"].to_pylist()):
                raise RuntimeError(f"Uncheckpointed scoped file has missing trade IDs: {file}")
            known[relative] = {"rows": table.num_rows, "bytes": file.stat().st_size}
            self.stats["recovered_files"] += 1
        if self.stats["recovered_files"]:
            self._save()

    def _local_markets(self, family: str) -> dict[str, dict]:
        files = sorted(p for p in self.markets_dir.glob("*.parquet") if not p.name.startswith("._"))
        if not files:
            return {}
        found = {}
        with duckdb.connect() as con:
            con.execute("SET threads=1")
            for index in range(0, len(files), 128):
                rows = con.execute(
                    "SELECT DISTINCT ticker,event_ticker FROM read_parquet(?, union_by_name=true) "
                    "WHERE starts_with(ticker, ?)",
                    [[str(p) for p in files[index:index + 128]], family + "-"],
                ).fetchall()
                for ticker, event in rows:
                    if _family_for_market(ticker, event, family):
                        found[ticker] = {"family": family, "event_ticker": event,
                                         "status": "local_catalog", "source": "local"}
        return found

    def _discovery(self, family: str, *, dry_run: bool) -> dict[str, dict]:
        existing = self.state["families"].get(family, {})
        found = {ticker: value for ticker, value in existing.get("discovered", {}).items()}
        if not existing.get("local_seeded"):
            for ticker, meta in self._local_markets(family).items():
                found.setdefault(ticker, meta)
        active = existing.get("discovery_active") if not dry_run else None
        if active is None:
            active = {name: {"cursor": None, "complete": False} for name in ("live", "historical")}
        if not dry_run:
            family_state = self.state["families"].setdefault(family, {})
            family_state["discovery_active"] = active
            family_state["discovered"] = found
            family_state["local_seeded"] = True
            self._save()
        for name, endpoint in (("live", "/markets"), ("historical", "/historical/markets")):
            source = active[name]
            seen = set()
            rewound = False
            while not source["complete"]:
                params = {"series_ticker": family, "limit": PAGE_LIMIT}
                if source["cursor"]:
                    params["cursor"] = source["cursor"]
                try:
                    response = self.client.http.get(endpoint, params=params)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 400 or not source["cursor"] or rewound:
                        raise
                    source["cursor"] = None
                    source["complete"] = False
                    seen.clear()
                    rewound = True
                    if not dry_run:
                        self._save()
                    continue
                if not isinstance(response, dict) or not isinstance(response.get("markets"), list):
                    raise TypeError(f"Invalid {name} market discovery response")
                for raw in response["markets"]:
                    ticker, event = raw.get("ticker"), raw.get("event_ticker")
                    if not _family_for_market(ticker, event, family):
                        raise ValueError(f"{name} discovery returned a market outside {family}")
                    found[ticker] = {"family": family, "event_ticker": event,
                                     "status": raw.get("status", "unknown"), "source": name,
                                     "close_time": raw.get("close_time"),
                                     "updated_at": raw.get("updated_time")}
                cursor = response.get("cursor") or None
                if cursor and (cursor == source["cursor"] or cursor in seen):
                    raise RuntimeError(f"Repeated cursor in {name} market discovery")
                if cursor:
                    seen.add(cursor)
                source["cursor"] = cursor
                source["complete"] = cursor is None
                if not dry_run:
                    family_state["discovered"] = found
                    self._save()  # Market records are durable before the cursor advances.
        if not dry_run:
            family_state["last_discovery_complete_at"] = datetime.now(timezone.utc).isoformat()
            family_state["discovery_active"] = None
            self._save()
        self.stats["discovered"] = len(found)
        return found

    def _exact_market(self, ticker: str) -> dict:
        family = ticker.split("-", 1)[0]
        try:
            market = self.client.get_market(ticker)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            market = self.client.get_market(ticker, historical=True)
        if market.ticker != ticker or not _family_for_market(market.ticker, market.event_ticker, family):
            raise ValueError("Exact market metadata does not match the selected ticker/family")
        return {"family": family, "event_ticker": market.event_ticker,
                "status": market.status, "source": "exact",
                "close_time": market.close_time.isoformat() if market.close_time else None}

    def _market_state(self, ticker: str, family: str) -> dict:
        markets = self.state["markets"]
        if ticker not in markets:
            markets[ticker] = {"family": family, "coverage": [], "active": None,
                               "requested_historical_start": None,
                               "baseline": {"source": "copy_only_migration", "historically_complete": False,
                                            "captured_through": None}}
            self.stats["new_markets"] += 1
            self._save()
        elif markets[ticker]["family"] != family:
            raise RuntimeError(f"Market {ticker} has conflicting family ownership")
        return markets[ticker]

    def _seed_ids(self, ids: sqlite3.Connection, ticker: str, family: str):
        files = sorted(self._partition(family).glob("*.parquet"))
        if not files:
            return
        with duckdb.connect() as con:
            con.execute("SET threads=1")
            for index in range(0, len(files), 64):
                cursor = con.execute(
                    "SELECT trade_id,ticker,count,yes_price,no_price,taker_side,epoch_ns(created_time) "
                    "FROM read_parquet(?, union_by_name=true) WHERE ticker = ?",
                    [[str(p) for p in files[index:index + 64]], ticker],
                )
                while batch := cursor.fetchmany(10_000):
                    for row in batch:
                        if not row[0]:
                            raise ValueError(f"Existing {ticker} row lacks trade_id")
                        self._remember(ids, row)
        ids.commit()

    @staticmethod
    def _remember(ids: sqlite3.Connection, row: tuple) -> bool:
        payload = _identity(row)
        previous = ids.execute("SELECT payload FROM seen WHERE trade_id=?", (row[0],)).fetchone()
        if previous:
            if previous[0] != payload:
                raise ValueError(f"Conflicting payload for trade_id {row[0]}")
            return False
        ids.execute("INSERT INTO seen VALUES (?,?)", (row[0], payload))
        return True

    def _space(self, extra=0):
        _check_space({"destination": str(self.destination / "_metadata"),
                      "spill": str(self.destination / "_metadata"),
                      "min_free_gib": self.min_free_gib}, extra=extra)

    def _write_batch(self, family: str, rows: list[dict]) -> str:
        self._space(extra=max(1_000_000, len(rows) * 256))
        folder = self._partition(family)
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"scoped_{uuid4().hex}.parquet"
        temporary = output.with_suffix(".parquet.tmp")
        try:
            table = pa.Table.from_pylist(rows, schema=self.schema)
            table = table.sort_by([("ticker", "ascending"), ("created_time", "ascending"),
                                   ("trade_id", "ascending")])
            pq.write_table(table, temporary, compression="zstd")
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, output)
            _fault("after_publish_before_checkpoint")
            return output.relative_to(self.destination).as_posix()
        finally:
            temporary.unlink(missing_ok=True)

    def _source(self, ids, ticker, family, active, source_name, endpoint):
        source = active["sources"][source_name]
        cursor = source["cursor"]
        seen_cursors = {cursor} if cursor else set()
        buffer = []
        pages_since_commit = 0
        while not source["complete"]:
            # Kalshi documents Unix seconds and describes these as after/before;
            # widen by one second, then enforce exact half-open bounds locally.
            params = {"ticker": ticker, "limit": PAGE_LIMIT,
                      "min_ts": max(0, active["start"] - 1), "max_ts": active["end"] + 1}
            if cursor:
                params["cursor"] = cursor
            try:
                response = self.client.http.get(endpoint, params=params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 400 and cursor:
                    raise ExpiredCursor(f"Expired {source_name} cursor for {ticker}") from exc
                raise
            if not isinstance(response, dict) or not isinstance(response.get("trades"), list):
                raise TypeError(f"Invalid trade response from {endpoint}")
            self.stats["fetched"] += len(response["trades"])
            for raw in response["trades"]:
                item = _normalized(raw, ticker, family, active["start"], active["end"], self.schema)
                if item is None:
                    continue
                row, identity = item
                if self._remember(ids, identity):
                    buffer.append(row)
                else:
                    self.stats["duplicate"] += 1
            pages_since_commit += 1
            next_cursor = response.get("cursor") or None
            if next_cursor and next_cursor in seen_cursors:
                raise RuntimeError(f"Repeated trade cursor for {ticker} {source_name}")
            if next_cursor:
                seen_cursors.add(next_cursor)
            if len(buffer) >= BATCH_SIZE or next_cursor is None or (not buffer and pages_since_commit >= 10):
                if buffer:
                    relative = self._write_batch(family, buffer)
                    self.state["files"][relative] = {"rows": len(buffer),
                                                     "bytes": (self.destination / relative).stat().st_size}
                    source["batches"].append(relative)
                    source["new"] += len(buffer)
                    self.stats["new"] += len(buffer)
                    buffer.clear()
                source["cursor"] = next_cursor
                source["complete"] = next_cursor is None
                source["pages"] += pages_since_commit
                self._save()
                ids.commit()
                pages_since_commit = 0
            cursor = next_cursor
        return

    def _window(self, ids, market: dict, ticker: str, family: str, start: int, end: int):
        active = market.get("active")
        if active is None:
            active = {"start": start, "end": end, "started_at": self.now(),
                      "sources": {name: {"cursor": None, "complete": False, "pages": 0,
                                         "new": 0, "batches": []} for name, _ in SOURCES}}
            market["active"] = active
            self._save()
        elif active["start"] != start or active["end"] != end:
            raise RuntimeError("Active scoped window does not match its stored bounds")
        if self.now() - active["started_at"] > CURSOR_RECHECK_SECONDS:
            self._rewind(active)
            active["started_at"] = self.now()
            self._save()
        rewinds = 0
        while True:
            try:
                for name, endpoint in SOURCES:
                    if not active["sources"][name]["complete"]:
                        self._source(ids, ticker, family, active, name, endpoint)
                break
            except ExpiredCursor:
                if rewinds:
                    raise
                self._rewind(active)
                active["started_at"] = self.now()
                self._save()
                ids.execute("DELETE FROM seen")
                ids.commit()
                self._seed_ids(ids, ticker, family)
                rewinds += 1
        market["coverage"] = _coverage_add(market["coverage"], start, end)
        market["last_fully_checked_end"] = max(last for _, last in market["coverage"])
        market["active"] = None
        self._save()  # Successful empty windows advance coverage too.

    @staticmethod
    def _rewind(active):
        for name, _ in SOURCES:
            source = active["sources"][name]
            source["cursor"] = None
            source["complete"] = False
            source["pages"] = 0
            source["new"] = 0
            source["batches"] = []

    def _process_market(self, ticker: str, meta: dict, since: int | None,
                        until: int, explicit: bool):
        family = meta["family"]
        market = self._market_state(ticker, family)
        if since is not None:
            previous = market["requested_historical_start"]
            market["requested_historical_start"] = min(previous, since) if previous is not None else since
            self._save()
        latest = max((end for _, end in market["coverage"]), default=None)
        if market["active"] is not None:
            active = market["active"]
            with tempfile.TemporaryDirectory(prefix="scoped_ids_", dir=self.destination / "_metadata") as temporary:
                ids = sqlite3.connect(Path(temporary) / "ids.sqlite")
                try:
                    ids.execute("CREATE TABLE seen (trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                    self._seed_ids(ids, ticker, family)
                    self._window(ids, market, ticker, family, active["start"], active["end"])
                    self._continue_windows(ids, market, ticker, family,
                                           since if explicit else active["end"], until)
                finally:
                    ids.close()
            return "updated"
        start = since if explicit else max(0, latest - int(OVERLAP.total_seconds())) if latest is not None else since
        if start is None:
            raise RuntimeError(f"{ticker} has no proven API coverage boundary; provide --since for initial adoption")
        if start >= until:
            return "already checked through requested end"
        self._space()
        with tempfile.TemporaryDirectory(prefix="scoped_ids_", dir=self.destination / "_metadata") as temporary:
            ids = sqlite3.connect(Path(temporary) / "ids.sqlite")
            try:
                ids.execute("CREATE TABLE seen (trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                self._seed_ids(ids, ticker, family)
                self._continue_windows(ids, market, ticker, family, start, until)
            finally:
                ids.close()
        return "updated"

    def _continue_windows(self, ids, market, ticker, family, start, until):
        while start < until:
            end = min(start + WINDOW_SECONDS, until)
            self._window(ids, market, ticker, family, start, end)
            start = end

    def run(self, *, family: str | None = None, ticker: str | None = None,
            since: int | None = None, until: int | None = None, dry_run=False) -> dict:
        mode, selected = validate_selection(family, ticker)
        frozen = self.now()
        if until is not None and since is None:
            raise ValueError("--until requires --since")
        if since is not None and (since < 0 or since >= (until if until is not None else frozen)):
            raise ValueError("--since must precede --until/current UTC time")
        if until is not None and until > frozen:
            raise ValueError("--until cannot exceed the run's fixed UTC end")
        self._published()
        self._load_state()
        if dry_run:
            return self._run_locked(mode, selected, since, until or frozen, dry_run=True)
        # A single dataset lock covers --family, --ticker, deduplication and writes.
        with _lock(self.destination / "_metadata"):
            self._load_state()
            selected_family = selected if mode == "family" else selected.split("-", 1)[0]
            self._reconcile_files(selected_family)
            return self._run_locked(mode, selected, since, until or frozen, dry_run=False)

    def _run_locked(self, mode, selected, since, until, *, dry_run):
        if mode == "family":
            found = self._discovery(selected, dry_run=dry_run)
            family_state = self.state["families"].get(selected, {})
            default_since = family_state.get("requested_start")
            if since is not None and not dry_run:
                family_state["requested_start"] = min(default_since, since) if default_since is not None else since
                self._save()
                default_since = family_state["requested_start"]
            tickers = sorted(found)
            metadata = found
        else:
            tickers = [selected]
            metadata = {selected: self._exact_market(selected)}
            default_since = self.state["families"].get(metadata[selected]["family"], {}).get("requested_start")
        plans = []
        baseline_counts = {}
        for market_ticker in tickers:
            item = self.state["markets"].get(market_ticker, {})
            latest = max((end for _, end in item.get("coverage", [])), default=None)
            planned_since = since if since is not None else max(0, latest - int(OVERLAP.total_seconds())) if latest is not None else default_since
            market_family = metadata[market_ticker]["family"]
            if market_family not in baseline_counts:
                baseline_counts[market_family] = self._baseline_rows(market_family)
            plans.append({"ticker": market_ticker, "family": market_family,
                          "baseline_family_rows": baseline_counts[market_family],
                          "covered": item.get("coverage", []), "active": item.get("active"),
                          "planned_start": planned_since, "planned_end": until})
        report = {"scope": {mode: selected}, "destination": str(self.destination),
                  "base_run_id": self.dataset["run_id"], "historical_completeness": "unknown before checked ranges",
                  "markets": len(tickers), "new_markets": sum(t not in self.state["markets"] for t in tickers),
                  "dry_run": dry_run, "plan": plans if dry_run else None, "stats": self.stats,
                  "pending_markets": []}
        if dry_run:
            return report
        for market_ticker in tickers:
            try:
                planned = since if since is not None else default_since
                result = self._process_market(market_ticker, metadata[market_ticker], planned,
                                              until, explicit=since is not None)
                print(f"{market_ticker}: {result}", flush=True)
            except Exception as exc:
                self.stats["failed"].append({"ticker": market_ticker, "error": str(exc)})
                report["pending_markets"].append(market_ticker)
                print(f"{market_ticker}: FAILED: {exc}", flush=True)
        return report

    def _baseline_rows(self, family):
        # The catalog remains the immutable migration snapshot; added counts
        # live in scoped_checkpoint.json rather than rewriting that provenance.
        catalog = self.destination / "_metadata" / "family_catalog.parquet"
        with duckdb.connect() as con:
            row = con.execute("SELECT row_count FROM read_parquet(?) WHERE family=?",
                              [str(catalog), family]).fetchone()
        return row[0] if row else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Incrementally collect one Kalshi trade family or market")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--family")
    group.add_argument("--ticker")
    parser.add_argument("--since", type=parse_time, help="UTC start for initial coverage or explicit repair")
    parser.add_argument("--until", type=parse_time, help="UTC end for explicit bounded repair")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-free-gib", type=float, default=25)
    args = parser.parse_args(argv)
    try:
        if args.min_free_gib < 0:
            raise ValueError("--min-free-gib must be nonnegative")
        with KalshiClient() as client:
            report = ScopedTrades(client, min_free_gib=args.min_free_gib).run(
                family=args.family, ticker=args.ticker, since=args.since,
                until=args.until, dry_run=args.dry_run)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 1 if report["stats"]["failed"] else 0
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        parser.exit(1, f"Scoped trade update failed: {exc}\n")

