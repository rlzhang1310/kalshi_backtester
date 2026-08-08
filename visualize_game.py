"""Open the interactive Kalshi single-game odds visualizer.

Examples:
    python visualize_game.py KXNFLGAME-25SEP07CINCLE
    python visualize_game.py KXNFLGAME-25SEP07CINCLE-CIN
    python visualize_game.py KXNFLGAME-25SEP07CINCLE --target CIN
"""

from __future__ import annotations

import argparse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Sequence

import httpx

from src.analysis.kalshi.single_game_odds_time_series import (
    build_game_odds_figure,
    figure_display_config,
    load_game_odds,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find a Kalshi game's child markets, load their trades, and open "
            "an interactive implied-probability chart."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "TICKER may be an event ticker or one of its child market tickers.\n"
            "ISO timestamps without an offset are interpreted as UTC."
        ),
    )
    parser.add_argument("ticker", help="Kalshi event ticker or child market ticker")
    parser.add_argument(
        "--target",
        help="Target outcome code, child ticker, or YES subtitle",
    )
    parser.add_argument(
        "--start",
        dest="start_time",
        help=(
            "Override the Kalshi game start; only include trades at/after "
            "this ISO timestamp"
        ),
    )
    parser.add_argument(
        "--end",
        dest="end_time",
        help="Only include trades at/before this ISO timestamp",
    )
    parser.add_argument(
        "--frequency",
        default="1s",
        help="Plot resampling interval (default: 1s)",
    )
    parser.add_argument(
        "--no-resample",
        action="store_true",
        help="Plot raw trades as a step line instead of resampling",
    )
    parser.add_argument(
        "--no-fees",
        action="store_true",
        help="Hide the maker and taker fee-adjusted lines",
    )
    parser.add_argument(
        "--renderer",
        default="browser",
        help="Plotly renderer used to show the figure (default: browser)",
    )
    parser.add_argument("--width", type=int, default=2500)
    parser.add_argument("--height", type=int, default=1300)
    parser.add_argument(
        "--output-html",
        type=Path,
        help=(
            "Standalone chart path (default: output/<event>_<target>_kalshi_chart.html)"
        ),
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="Build/save the chart without opening a browser window",
    )
    return parser.parse_args(argv)


def open_chart_in_browser(html: bytes) -> str:
    """Serve one chart request on loopback, print its URL, and open it."""

    class ChartRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, _format: str, *args) -> None:
            del args

    with HTTPServer(("127.0.0.1", 0), ChartRequestHandler) as server:
        server.timeout = 60
        chart_url = f"http://127.0.0.1:{server.server_port}/"
        print(f"Chart link: {chart_url}", flush=True)
        webbrowser.open(chart_url, new=2)
        server.handle_request()

    return chart_url


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        game = load_game_odds(
            args.ticker,
            target=args.target,
            start_time=args.start_time,
            end_time=args.end_time,
        )
        frequency = None if args.no_resample else args.frequency
        figure = build_game_odds_figure(
            game.compiled,
            event_ticker=game.event_ticker,
            target_label=game.target_label,
            resample_frequency=frequency,
            show_fee_lines=not args.no_fees,
            width=args.width,
            height=args.height,
        )
    except (ValueError, httpx.HTTPError) as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"Event: {game.event_ticker}")
    print("Markets:")
    for market in game.markets:
        selected = "  <-- target" if market.ticker == game.target_ticker else ""
        label = market.yes_sub_title or market.ticker
        print(f"  {market.ticker} | {label}{selected}")
    print(f"Trades: {len(game.trades):,}")
    print(f"Plotted side: {game.target_label} ({game.target_side})")
    start_source = (
        "Kalshi API" if game.start_time_source == "kalshi_milestone" else "provided"
    )
    print(f"Start: {game.start_time.isoformat()} ({start_source})")

    display_config = figure_display_config(
        game.event_ticker,
        game.target_label,
        width=args.width,
        height=args.height,
    )
    default_filename = display_config["toImageButtonOptions"]["filename"] + ".html"
    requested_output = args.output_html or Path("output") / default_filename
    output_path = requested_output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    figure.write_html(
        output_path,
        include_plotlyjs=True,
        full_html=True,
        config=display_config,
    )
    if args.no_show:
        print(f"Chart file: {output_path.as_uri()}")
    elif args.renderer == "browser":
        open_chart_in_browser(output_path.read_bytes())
    else:
        print(f"Chart file: {output_path.as_uri()}")
        figure.show(renderer=args.renderer, config=display_config)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
