"""DuckDB helpers for reading the legacy and global Kalshi trade datasets."""

from __future__ import annotations

from pathlib import Path

import duckdb


def create_combined_trades_view(
    con: duckdb.DuckDBPyConnection,
    trades_dir: Path,
    global_trades_dir: Path,
) -> None:
    """Create ``analysis_trades`` from every global staging trade file.

    Historical and live staging files are treated as disjoint and concatenated
    without trade-ID deduplication.
    ``trades_dir`` remains in the signature so existing analysis constructors
    stay backward compatible.
    """
    del trades_dir
    staging_glob = _sql_literal(str(global_trades_dir / "*.parquet"))

    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW analysis_trades AS
        SELECT trade_id, ticker, count, yes_price, no_price, taker_side, created_time
        FROM read_parquet(
            '{staging_glob}',
            union_by_name = true
        )
        """
    )


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")
