"""Print the schema and a few records from the stored Kalshi trade feed.

Examples:
    python scripts/inspect_kalshi_trade.py
    python scripts/inspect_kalshi_trade.py --limit 10
    python scripts/inspect_kalshi_trade.py --ticker KXNBAGAME-...
    python scripts/inspect_kalshi_trade.py --path "data/kalshi/trades/*.parquet"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb


DEFAULT_PATH = "data/kalshi/trades_global_staging/*.parquet"


def sql_literal(value: str) -> str:
    """Escape a value embedded as a DuckDB string literal."""
    return value.replace("'", "''")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show what records in the stored Kalshi trade feed look like."
    )
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help=f"Parquet file or glob to inspect (default: {DEFAULT_PATH})",
    )
    parser.add_argument("--ticker", help="Only show trades for this exact ticker")
    parser.add_argument("--limit", type=int, default=5, help="Rows to print (default: 5)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")

    parquet_path = Path(args.path).as_posix()
    source = f"read_parquet('{sql_literal(parquet_path)}')"
    where = ""
    parameters: list[str] = []
    if args.ticker:
        where = "WHERE ticker = ?"
        parameters.append(args.ticker)

    con = duckdb.connect()

    print(f"Source: {parquet_path}\n")
    print("Schema:")
    schema = con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    for column_name, column_type, *_ in schema:
        print(f"  {column_name}: {column_type}")

    query = f"""
        SELECT *, yes_price + no_price AS price_sum
        FROM {source}
        {where}
        ORDER BY created_time DESC
        LIMIT {args.limit}
    """
    rows = con.execute(query, parameters).fetchdf()

    print(f"\nSample records ({len(rows)}):")
    if rows.empty:
        print("  No matching trades found.")
        return

    for number, record in enumerate(rows.to_dict(orient="records"), start=1):
        # default=str makes timestamps and other DuckDB/Pandas values readable.
        print(f"\nTrade {number}:")
        print(json.dumps(record, indent=2, default=str))

    bad_complements = int((rows["price_sum"] != 100).sum())
    print(
        f"\nPrice check: {bad_complements}/{len(rows)} sampled trades have "
        "yes_price + no_price != 100."
    )


if __name__ == "__main__":
    main()
