"""Indexer for Kalshi trades data."""

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd
from tqdm import tqdm

from src.common.indexer import Indexer
from src.indexers.kalshi.client import KalshiClient

DATA_DIR = Path("data/kalshi/trades")
MARKETS_DIR = Path("data/kalshi/markets")
CURSOR_FILE = Path("data/kalshi/.backfill_trades_cursor")


class KalshiTradesIndexer(Indexer):
    """Fetches and stores Kalshi trades data."""

    def __init__(
        self,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        max_workers: int = 10,
    ):
        super().__init__(
            name="kalshi_trades",
            description="Backfills Kalshi trades data to parquet files",
        )
        self._min_ts = min_ts
        self._max_ts = max_ts
        self._max_workers = max_workers

    def run(self) -> None:
        BATCH_SIZE = 10000
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)

        # A ticker with saved trades is already complete. Loading every trade ID
        # into Python used tens of gigabytes on large backfills and is unnecessary:
        # tickers selected below cannot overlap with the saved-trade tickers.
        existing_tickers: set[str] = set()
        parquet_files = list(DATA_DIR.glob("trades_*.parquet"))
        if parquet_files:
            print("Loading processed market tickers...")
            try:
                result = duckdb.sql(
                    f"SELECT DISTINCT ticker FROM '{DATA_DIR}/trades_*.parquet'"
                ).fetchall()
                existing_tickers = {row[0] for row in result}
                print(f"Found {len(existing_tickers)} processed markets")
            except Exception as exc:
                print(f"Warning: could not load processed market tickers: {exc}")

        all_tickers = duckdb.sql(f"""
            SELECT DISTINCT ticker FROM '{MARKETS_DIR}/markets_*_*.parquet'
            WHERE volume >= 100
            ORDER BY ticker
        """).fetchall()
        all_tickers = [row[0] for row in all_tickers]
        print(f"Found {len(all_tickers)} unique markets")

        # Filter to tickers not fully processed
        tickers_to_process = [t for t in all_tickers if t not in existing_tickers]
        print(
            f"Skipped {len(all_tickers) - len(tickers_to_process)} already processed, "
            f"{len(tickers_to_process)} to fetch"
        )

        if not tickers_to_process:
            print("Nothing to process")
            return

        all_trades: list[dict] = []
        total_trades_saved = 0
        next_chunk_idx = 0

        # Calculate next chunk index
        if parquet_files:
            indices = []
            for f in parquet_files:
                parts = f.stem.split("_")
                if len(parts) >= 2:
                    try:
                        indices.append(int(parts[1]))
                    except ValueError:
                        pass
            if indices:
                next_chunk_idx = max(indices) + BATCH_SIZE

        def save_batch(trades_batch: list[dict]) -> int:
            nonlocal next_chunk_idx
            if not trades_batch:
                return 0
            chunk_path = DATA_DIR / f"trades_{next_chunk_idx}_{next_chunk_idx + BATCH_SIZE}.parquet"
            df = pd.DataFrame(trades_batch)
            df.to_parquet(chunk_path)
            next_chunk_idx += BATCH_SIZE
            return len(trades_batch)

        def fetch_ticker_trades(ticker: str) -> list[dict]:
            """Fetch trades for a single ticker."""
            client = KalshiClient()
            try:
                trades = client.get_market_trades(
                    ticker,
                    verbose=False,
                    min_ts=self._min_ts,
                    max_ts=self._max_ts,
                )
                if not trades:
                    return []
                fetched_at = datetime.utcnow()
                return [{**asdict(t), "_fetched_at": fetched_at} for t in trades]
            finally:
                client.close()

        # Keep only a bounded number of futures queued. Submitting millions of
        # futures up front can consume tens of gigabytes before progress reaches 1%.
        pbar = tqdm(total=len(tickers_to_process), desc="Fetching trades")
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            ticker_iter = iter(tickers_to_process)
            max_pending = max(self._max_workers * 4, self._max_workers)
            pending: dict[Future[list[dict]], str] = {}

            def submit_next() -> bool:
                try:
                    ticker = next(ticker_iter)
                except StopIteration:
                    return False
                pending[executor.submit(fetch_ticker_trades, ticker)] = ticker
                return True

            for _ in range(min(max_pending, len(tickers_to_process))):
                submit_next()

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    ticker = pending.pop(future)
                    try:
                        trades_data = future.result()
                        if trades_data:
                            all_trades.extend(trades_data)

                        pbar.set_postfix(
                            buffer=len(all_trades),
                            saved=total_trades_saved,
                            last=ticker[-20:],
                        )

                        # Save in batches
                        while len(all_trades) >= BATCH_SIZE:
                            saved = save_batch(all_trades[:BATCH_SIZE])
                            total_trades_saved += saved
                            all_trades = all_trades[BATCH_SIZE:]

                    except Exception as e:
                        tqdm.write(f"Error fetching {ticker}: {e}")
                    finally:
                        pbar.update(1)
                        submit_next()

        pbar.close()

        # Save remaining
        if all_trades:
            total_trades_saved += save_batch(all_trades)

        print(
            f"\nBackfill trades complete: {len(tickers_to_process)} markets processed, "
            f"{total_trades_saved} trades saved"
        )
