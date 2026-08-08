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
    The price on ``taker_side`` is treated as authoritative and the other side
    is derived as its complement.  This repairs stored rows whose independently
    parsed YES and NO prices do not add up to 100 cents.
    ``trades_dir`` remains in the signature so existing analysis constructors
    stay backward compatible.
    """
    del trades_dir
    staging_glob = _sql_literal(str(global_trades_dir / "*.parquet"))

    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW analysis_trades AS
        WITH stored_trades AS (
            SELECT
                trade_id,
                ticker,
                count,
                yes_price,
                no_price,
                lower(taker_side) AS taker_side,
                created_time
            FROM read_parquet(
                '{staging_glob}',
                union_by_name = true
            )
        ),
        validated_trades AS (
            SELECT *,
                CASE
                    WHEN taker_side = 'yes' THEN yes_price
                    WHEN taker_side = 'no' THEN no_price
                END AS taker_price
            FROM stored_trades
        )
        SELECT
            trade_id,
            ticker,
            count,
            CASE
                WHEN taker_side = 'yes' THEN taker_price
                ELSE 100 - taker_price
            END AS yes_price,
            CASE
                WHEN taker_side = 'no' THEN taker_price
                ELSE 100 - taker_price
            END AS no_price,
            taker_side,
            created_time
        FROM validated_trades
        WHERE taker_side IN ('yes', 'no')
          AND taker_price BETWEEN 0 AND 100
        """
    )


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")
