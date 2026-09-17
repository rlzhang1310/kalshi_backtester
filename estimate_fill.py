"""Prepare or query historical bid-fill proxies; run without arguments for a menu."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import httpx
import pandas as pd

from src.analysis.kalshi.fill_probability import (
    estimate_fill_probability,
    normalize_family,
)
from src.analysis.kalshi.fill_probability_data import prepare_fill_data
from src.analysis.kalshi.local_game_data import DEFAULT_DATA_DIR


DEFAULT_FAMILY = "KXATPCHALLENGERMATCH"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    prepare = commands.add_parser(
        "prepare", help="Normalize every locally stored ticker in a family"
    )
    query = commands.add_parser(
        "query", help="Estimate optimistic/conservative fill rates from saved states"
    )
    for command in (prepare, query):
        command.add_argument("--family", default=DEFAULT_FAMILY)
        command.add_argument(
            "--output-dir", type=Path, default=Path("output/fill_probability")
        )
    prepare.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    prepare.add_argument(
        "--timings",
        type=Path,
        help="Optional CSV/Parquet with ticker,event_ticker,start_time,settlement_time,close_time",
    )
    prepare.add_argument("--time-step", type=float, default=0.05)
    prepare.add_argument("--max-clv-age-minutes", type=float)
    prepare.add_argument(
        "--refresh",
        action="store_true",
        help="Rescan collected data and refetch API metadata",
    )
    query.add_argument(
        "--ticker", help="Exclude this ticker's entire event from reference data"
    )
    query.add_argument(
        "--clv", type=float, required=True, help="Pregame YES probability, from 0 to 1"
    )
    query.add_argument(
        "--x", type=float, required=True, help="Current YES price, from 0 to 1"
    )
    query.add_argument(
        "--t",
        type=float,
        required=True,
        help="Retrospective normalized time, from 0 to 1",
    )
    query.add_argument(
        "--y", type=float, required=True, help="YES bid price, strictly below x"
    )
    query.add_argument("--clv-tolerance", type=float, default=0.05)
    query.add_argument("--price-tolerance", type=float, default=0.05)
    query.add_argument("--time-tolerance", type=float, default=0.025)
    query.add_argument("--min-events", type=int, default=20)
    query.add_argument(
        "--as-of", help="Use only reference events settled before this ISO timestamp"
    )
    return parser.parse_args(argv)


def menu_args():
    print("\nBid fill probability")
    print("  1. Prepare a ticker family (first use / refresh inputs)")
    print("  2. Estimate a bid's fill probability")
    print("  q. Quit")
    while True:
        choice = input("Choose [2]: ").strip().lower() or "2"
        if choice == "q":
            return None
        if choice in ("1", "2"):
            break
        print("Choose 1, 2, or q.")
    family = input(f"Ticker family [{DEFAULT_FAMILY}]: ").strip() or DEFAULT_FAMILY
    if choice == "1":
        return parse_args(["prepare", "--family", family])
    ticker = input(
        "Specific event/market ticker (optional, excludes its game): "
    ).strip()
    argv = ["query", "--family", family]
    if ticker:
        argv += ["--ticker", ticker]
    for option, label in [
        ("--clv", "Pregame probability / CLV"),
        ("--x", "Current price x"),
        ("--t", "Normalized time t"),
        ("--y", "Bid price y (below x)"),
    ]:
        while True:
            raw = input(f"{label} [0 to 1]: ").strip()
            try:
                value = float(raw)
            except ValueError:
                value = -1
            if 0 <= value <= 1:
                argv += [option, raw]
                break
            print("Enter a decimal from 0 to 1, such as 0.60.")
    return parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.command is None:
            args = menu_args()
            if args is None:
                return 0
        family = normalize_family(args.family)
        if args.command == "prepare":
            prepare_fill_data(
                family=family,
                data_dir=args.data_dir,
                output_dir=args.output_dir,
                timings_path=args.timings,
                time_step=args.time_step,
                max_clv_age_minutes=args.max_clv_age_minutes,
                refresh=args.refresh,
            )
            return 0
        folder = args.output_dir / family
        path = folder / "snapshots.parquet"
        if not path.exists():
            raise ValueError(
                f"No prepared data for {family}. Choose menu option 1 or run: python estimate_fill.py prepare --family {family}"
            )
        snapshots = pd.read_parquet(path)
        result = estimate_fill_probability(
            snapshots,
            family=family,
            clv=args.clv,
            current_price=args.x,
            normalized_time=args.t,
            bid_price=args.y,
            ticker=args.ticker,
            clv_tolerance=args.clv_tolerance,
            price_tolerance=args.price_tolerance,
            time_tolerance=args.time_tolerance,
            min_events=args.min_events,
            as_of=args.as_of,
        )
        if result["status"] in ("ok", "horizon_ended"):
            print(f"Conservative (trade through): {result['p_conservative']:.2%}")
            print(f"Optimistic (touch):           {result['p_optimistic']:.2%}")
            print(f"Touch-only gap:               {result['fill_probability_gap']:.2%}")
        else:
            print(
                f"Insufficient support: {result['n_events']} matches; at least {args.min_events} required."
            )
        print(
            f"Support: {result['n_events']:,} events / {result['n_tickers']:,} tickers / {result['n_observations']:,} sampled states"
        )
        print(
            "Time uses realized settlement; these are historical execution proxies, without queue or order-size modeling."
        )
        output = folder / "last_estimate.json"
        output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Saved: {output}")
        return 0
    except (ValueError, OSError, duckdb.Error, httpx.HTTPError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Cancelled.") from None


if __name__ == "__main__":
    raise SystemExit(main())
