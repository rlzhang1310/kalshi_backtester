"""Append-only, resumable refresh of an already completed global trade backfill."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from itertools import islice
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd

from src.common.indexer import Indexer
from src.indexers.kalshi.client import KalshiClient
from src.indexers.kalshi.global_trades import (
    BATCH_SIZE,
    DATA_DIR,
    PAGE_LIMIT,
)
from src.indexers.kalshi.models import Trade


REFRESH_CHECKPOINT = DATA_DIR / "refresh_checkpoint.json"
BACKFILL_CHECKPOINT = DATA_DIR / "checkpoint.json"
LOCK_FILE = DATA_DIR / "refresh.lock"
OVERLAP = timedelta(days=7)
# Read live first, then historical: trades can move to the historical tier.
SOURCES = (("live", "/markets/trades"), ("historical", "/historical/trades"))


def _save_checkpoint(checkpoint: dict) -> None:
    temporary = REFRESH_CHECKPOINT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(checkpoint, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(REFRESH_CHECKPOINT)


def _initial_start() -> int:
    if not BACKFILL_CHECKPOINT.exists():
        raise RuntimeError("Complete the Kalshi Global Trades backfill before refreshing it.")
    original = json.loads(BACKFILL_CHECKPOINT.read_text(encoding="utf-8"))
    states = original["sources"]
    if not all(states[name]["completed"] for name, _ in SOURCES):
        raise RuntimeError("The original global trade feeds are not both complete yet.")
    # updated_at is a job timestamp, not a data watermark. Start before the
    # earliest finished feed and reconcile the overlap by trade_id.
    finished = [datetime.fromisoformat(states[name]["updated_at"]) for name, _ in SOURCES]
    return max(0, int((min(finished) - OVERLAP).timestamp()))


def _load_or_start_checkpoint() -> dict:
    if REFRESH_CHECKPOINT.exists():
        checkpoint = json.loads(REFRESH_CHECKPOINT.read_text(encoding="utf-8"))
    else:
        checkpoint = {"version": 1, "last_completed_end_ts": None, "active": None}
    if checkpoint["active"] is None:
        previous_end = checkpoint["last_completed_end_ts"]
        start = max(0, previous_end - int(OVERLAP.total_seconds())) if previous_end else _initial_start()
        end = int(datetime.now(timezone.utc).timestamp())
        if start >= end:
            raise RuntimeError("Refresh start must precede the current UTC time.")
        checkpoint["active"] = {
            "min_ts": start,
            "max_ts": end,
            "sources": {name: {"cursor": None, "completed": False, "pages": 0, "added": 0}
                        for name, _ in SOURCES},
        }
        _save_checkpoint(checkpoint)
    return checkpoint


def _seed_ids(ids: sqlite3.Connection, minimum: int, maximum: int) -> None:
    """Index archive IDs in the bounded refresh range, on disk rather than RAM."""
    files = (str(path) for path in DATA_DIR.glob("*.parquet")
             if not path.name.startswith("._"))
    archive = duckdb.connect()
    try:
        while group := list(islice(files, 512)):
            rows = archive.execute(
                "SELECT trade_id FROM read_parquet(?, union_by_name=true) "
                "WHERE created_time >= ? AND created_time <= ?",
                [group, datetime.fromtimestamp(minimum, timezone.utc),
                 datetime.fromtimestamp(maximum, timezone.utc)],
            )
            while batch := rows.fetchmany(10_000):
                ids.executemany("INSERT OR IGNORE INTO seen (trade_id) VALUES (?)", batch)
            ids.commit()
    finally:
        archive.close()


def _write_batch(source: str, records: list[dict]) -> None:
    # A fresh name on every attempt means a crash cannot overwrite a batch
    # committed just before its checkpoint was saved.
    while True:
        output = DATA_DIR / f"refresh_{source}_{uuid4().hex}.parquet"
        if not output.exists():
            break
    temporary = output.with_suffix(".parquet.tmp")
    try:
        pd.DataFrame(records).to_parquet(temporary, index=False)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


class KalshiGlobalTradesRefreshIndexer(Indexer):
    """Collect only missing trade IDs in a bounded, overlapping time range."""

    def __init__(self) -> None:
        super().__init__(
            name="kalshi_global_trades_refresh",
            description="Append new global trades without restarting the completed backfill",
        )

    def run(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            lock = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(
                f"Refresh lock exists at {LOCK_FILE}; check for another run before clearing it."
            ) from exc
        os.close(lock)
        try:
            self._run_locked()
        finally:
            LOCK_FILE.unlink(missing_ok=True)

    def _run_locked(self) -> None:
        # Validate archive and completed backfill before creating a refresh
        # checkpoint; an active refresh retains its original fixed bounds.
        _initial_start()
        if not any(DATA_DIR.glob("*.parquet")):
            raise RuntimeError("No global trade Parquet files found.")
        checkpoint = _load_or_start_checkpoint()
        active = checkpoint["active"]
        print(f"Refreshing global trades from {active['min_ts']} to {active['max_ts']} (UTC Unix seconds)")

        with tempfile.TemporaryDirectory(prefix="kalshi_refresh_ids_") as temporary:
            ids = sqlite3.connect(Path(temporary) / "ids.sqlite")
            try:
                ids.execute("CREATE TABLE seen (trade_id TEXT PRIMARY KEY)")
                _seed_ids(ids, active["min_ts"], active["max_ts"])
                with KalshiClient() as client:
                    for source, endpoint in SOURCES:
                        if not active["sources"][source]["completed"]:
                            self._refresh_source(client, ids, checkpoint, source, endpoint)
            finally:
                ids.close()

        checkpoint["last_completed_end_ts"] = active["max_ts"]
        checkpoint["active"] = None
        _save_checkpoint(checkpoint)
        print("Incremental global trade refresh complete.")

    @staticmethod
    def _refresh_source(client, ids, checkpoint: dict, source: str, endpoint: str) -> None:
        active = checkpoint["active"]
        state = active["sources"][source]
        cursor = state["cursor"]
        seen_cursors = {cursor} if cursor else set()
        buffer: list[dict] = []
        buffered_pages = 0
        while True:
            params = {"limit": PAGE_LIMIT, "min_ts": active["min_ts"], "max_ts": active["max_ts"]}
            if cursor:
                params["cursor"] = cursor
            response = client.http.get(endpoint, params=params)
            if not isinstance(response, dict) or not isinstance(response.get("trades"), list):
                raise TypeError(f"Unexpected trade response from {endpoint}")
            for raw in response["trades"]:
                trade = Trade.from_dict(raw)
                if not trade.trade_id:
                    raise ValueError("Trade without an ID; refusing to save undeduplicable data.")
                if not active["min_ts"] <= trade.created_time.timestamp() <= active["max_ts"]:
                    raise ValueError(f"Trade outside requested refresh range from {endpoint}.")
                inserted = ids.execute(
                    "INSERT OR IGNORE INTO seen (trade_id) VALUES (?)", (trade.trade_id,)
                ).rowcount
                if inserted:
                    buffer.append({**asdict(trade), "_fetched_at": datetime.now(timezone.utc)})
            buffered_pages += 1
            next_cursor = response.get("cursor") or None
            if next_cursor is not None and next_cursor in seen_cursors:
                raise RuntimeError(f"Repeated pagination cursor from {endpoint}")
            if next_cursor is not None:
                seen_cursors.add(next_cursor)
            if len(buffer) >= BATCH_SIZE or next_cursor is None:
                if buffer:
                    _write_batch(source, buffer)
                    state["added"] += len(buffer)
                    buffer.clear()
                state["cursor"] = next_cursor
                state["completed"] = next_cursor is None
                state["pages"] += buffered_pages
                _save_checkpoint(checkpoint)
                buffered_pages = 0
                ids.commit()
                print(f"{source}: {state['pages']} pages, {state['added']} new trades", flush=True)
            if next_cursor is None:
                break
            cursor = next_cursor
