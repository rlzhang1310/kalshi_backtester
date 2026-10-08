"""Print the schema and a few records from the stored Kalshi trade feed.

Examples:
    python scripts/inspect_kalshi_trade.py --family KXNFLGAME
    python scripts/inspect_kalshi_trade.py --ticker KXNFLGAME-... --limit 10
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analysis.kalshi.util.trades import family_trade_files


DEFAULT_DATASET = Path("data/kalshi/trades_by_series")


def sql_literal(value: str) -> str:
    """Escape a value embedded as a DuckDB string literal."""
    return value.replace("'", "''")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show what records in the stored Kalshi trade feed look like."
    )
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--family", help="One ticker family")
    selection.add_argument("--ticker", help="One full market ticker")
    parser.add_argument("--limit", type=int, default=5, help="Rows to print (default: 5)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if args.ticker:
        args.ticker = args.ticker.strip().upper()
        if "-" not in args.ticker:
            raise SystemExit("--ticker requires a full market ticker; use --family for a series")

    family = args.family or args.ticker.split("-", 1)[0]
    files = family_trade_files(args.dataset_dir, family)
    if not files:
        raise SystemExit(f"No published trades for {family}")
    parquet_path = str(args.dataset_dir / family)
    paths = ", ".join(f"'{sql_literal(str(path))}'" for path in files)
    source = f"read_parquet([{paths}], union_by_name=true, hive_partitioning=false)"
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
