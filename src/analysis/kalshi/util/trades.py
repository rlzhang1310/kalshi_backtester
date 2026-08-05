"""DuckDB helpers for reading the legacy and global Kalshi trade datasets."""

from __future__ import annotations

from pathlib import Path

import duckdb


def create_combined_trades_view(
    con: duckdb.DuckDBPyConnection,
    trades_dir: Path,
    global_trades_dir: Path,
) -> None:
    """Create ``analysis_trades`` from the new global live-feed backfill.

    The historical and live staging feeds overlap, so analyses intentionally
    use only ``live_*.parquet`` rather than combining both staging sources.
    ``trades_dir`` remains in the signature so existing analysis constructors
    stay backward compatible while this staging-only approach is evaluated.
    """
    del trades_dir
    live_glob = _sql_literal(str(global_trades_dir / "live_*.parquet"))

    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW analysis_trades AS
        SELECT trade_id, ticker, count, yes_price, no_price, taker_side, created_time
        FROM read_parquet('{live_glob}')
        """
    )


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")
