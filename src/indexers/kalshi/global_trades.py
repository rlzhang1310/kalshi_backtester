"""Global, cursor-paginated indexer for all Kalshi trades.

This is intentionally separate from ``trades.py`` so the original per-market
backfill remains available. Data is staged separately and can be validated and
deduplicated against the original dataset before any migration.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.common.indexer import Indexer
from src.indexers.kalshi.client import KalshiClient
from src.indexers.kalshi.models import Trade


DATA_DIR = Path("data/kalshi/trades_global_staging")
CHECKPOINT_FILE = DATA_DIR / "checkpoint.json"
PAGE_LIMIT = 1000
BATCH_SIZE = 10_000

SOURCES = (
    ("historical", "/historical/trades"),
    ("live", "/markets/trades"),
)


class KalshiGlobalTradesIndexer(Indexer):
    """Backfill Kalshi trades by paginating the global trade feeds."""

    def __init__(self, min_ts: int | None = None, max_ts: int | None = None):
        super().__init__(
            name="kalshi_global_trades",
            description="Backfills global Kalshi trades into a separate staging folder",
        )
        self._min_ts = min_ts
        self._max_ts = max_ts

    def run(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        checkpoint = self._load_checkpoint()

        with KalshiClient() as client:
            for source_name, endpoint in SOURCES:
                state = checkpoint["sources"][source_name]
                if state["completed"]:
                    print(f"Skipping completed {source_name} trade feed")
                    continue

                self._backfill_source(
                    client=client,
                    source_name=source_name,
                    endpoint=endpoint,
                    checkpoint=checkpoint,
                )

        print(f"Global trade backfill complete. Staged files are in {DATA_DIR}")

    def _backfill_source(
        self,
        *,
        client: KalshiClient,
        source_name: str,
        endpoint: str,
        checkpoint: dict[str, Any],
    ) -> None:
        state = checkpoint["sources"][source_name]
        cursor: str | None = state["cursor"]
        next_batch: int = state["next_batch"]
        buffer: list[dict[str, Any]] = []
        pages_since_flush = 0

        print(
            f"Fetching {source_name} trades"
            + (" from checkpoint" if cursor else " from the beginning")
        )
        progress = tqdm(desc=f"Fetching {source_name} trades", unit=" trades")

        while True:
            params: dict[str, Any] = {"limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            if self._min_ts is not None:
                params["min_ts"] = self._min_ts
            if self._max_ts is not None:
                params["max_ts"] = self._max_ts

            response = client.http.get(endpoint, params=params)
            if not isinstance(response, dict):
                raise TypeError(f"Unexpected response from {endpoint}: {type(response)}")

            raw_trades = response.get("trades", [])
            fetched_at = datetime.now(timezone.utc)
            buffer.extend(
                {**asdict(Trade.from_dict(raw_trade)), "_fetched_at": fetched_at}
                for raw_trade in raw_trades
            )
            progress.update(len(raw_trades))
            pages_since_flush += 1

            next_cursor = response.get("cursor") or None
            reached_end = next_cursor is None

            # The cursor is checkpointed only after every trade fetched through
            # that cursor has reached disk. This prevents gaps after interruption.
            if len(buffer) >= BATCH_SIZE or reached_end:
                if buffer:
                    self._write_batch(source_name, next_batch, buffer)
                    next_batch += 1
                    buffer.clear()

                state.update(
                    cursor=next_cursor,
                    next_batch=next_batch,
                    completed=reached_end,
                    pages=state["pages"] + pages_since_flush,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                self._write_checkpoint(checkpoint)
                pages_since_flush = 0
                progress.set_postfix(batches=next_batch, refresh=False)

            if reached_end:
                break
            cursor = next_cursor

        progress.close()
        print(
            f"Completed {source_name} feed: "
            f"{state['pages']} pages, {state['next_batch']} parquet batches"
        )

    @staticmethod
    def _write_batch(
        source_name: str,
        batch_number: int,
        trades: list[dict[str, Any]],
    ) -> None:
        output_path = DATA_DIR / f"{source_name}_{batch_number:08d}.parquet"
        temporary_path = output_path.with_suffix(".parquet.tmp")
        pd.DataFrame(trades).to_parquet(temporary_path, index=False)
        temporary_path.replace(output_path)

    def _load_checkpoint(self) -> dict[str, Any]:
        if CHECKPOINT_FILE.exists():
            checkpoint = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
            expected_range = {
                "min_ts": self._min_ts,
                "max_ts": self._max_ts,
            }
            if checkpoint.get("time_range") != expected_range:
                raise RuntimeError(
                    "The existing global-trades checkpoint uses a different "
                    "min_ts/max_ts range. Move or remove the staging directory "
                    "before starting a different backfill range."
                )
            return checkpoint

        return {
            "version": 1,
            "time_range": {"min_ts": self._min_ts, "max_ts": self._max_ts},
            "sources": {
                source_name: {
                    "cursor": None,
                    "next_batch": 0,
                    "completed": False,
                    "pages": 0,
                    "updated_at": None,
                }
                for source_name, _ in SOURCES
            },
        }

    @staticmethod
    def _write_checkpoint(checkpoint: dict[str, Any]) -> None:
        temporary_path = CHECKPOINT_FILE.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(CHECKPOINT_FILE)
