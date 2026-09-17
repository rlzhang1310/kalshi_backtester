"""Open the interactive Kalshi single-game odds visualizer.

Examples:
    python visualize_game.py
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
import duckdb

from src.analysis.kalshi.local_game_data import (
    DEFAULT_DATA_DIR,
    LocalGameStore,
    list_local_games,
)
from src.analysis.kalshi.game_overlays import load_game_overlays

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
    parser.add_argument(
        "ticker",
        nargs="?",
        help="Kalshi event/market ticker; omit to open the game menu",
    )
    parser.add_argument(
        "--source",
        choices=["local", "api"],
        help="Choose the data source in the menu; explicit tickers default to api",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory containing kalshi/, sportsbook/, and playbyplay/",
    )
    parser.add_argument(
        "--sport", help="Filter the local game picker (e.g. nfl, nba, mlb)"
    )
    parser.add_argument(
        "--search", default="", help="Filter local games by ticker, team, or title"
    )
    parser.add_argument(
        "--list-games",
        action="store_true",
        help="Print matching local game tickers without plotting",
    )
    parser.add_argument(
        "--sportsbook", type=Path, help="Optional sportsbook CSV or Parquet file"
    )
    parser.add_argument(
        "--playbyplay", type=Path, help="Optional play-by-play CSV or Parquet file"
    )
    parser.add_argument(
        "--sportsbook-team",
        help="Selected team's exact name in a long-form sportsbook file",
    )
    parser.add_argument(
        "--sportsbook-side",
        choices=["home", "away"],
        help="Selected team's side in SportsDataIO moneyline files",
    )
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


def choose_game(
    data_dir: Path,
    *,
    source: str | None = None,
    sport: str | None = None,
    search: str = "",
) -> tuple[str, str]:
    """Choose a game and data source without needing command-line arguments."""
    print("\nSingle-game odds visualizer")
    print("  1. Enter a game or market ticker")
    print("  2. Find a game by team or name")
    print("  q. Quit")
    while True:
        choice = input("Choose an option [1]: ").strip().casefold() or "1"
        if choice == "q":
            raise SystemExit(0)
        if choice == "1":
            while True:
                ticker = input("Game or market ticker (q to quit): ").strip().upper()
                if ticker == "Q":
                    raise SystemExit(0)
                if ticker:
                    break
                print("Enter the game's event ticker or a team's market ticker.")
            selected_source = source
            if selected_source is None:
                print("\nLoad odds from:")
                print("  1. Local data")
                print("  2. Kalshi API")
                while True:
                    selected = (
                        input("Data source [2] (q to quit): ").strip().casefold() or "2"
                    )
                    if selected == "q":
                        raise SystemExit(0)
                    if selected in ("1", "2", "local", "api"):
                        selected_source = (
                            "local" if selected in ("1", "local") else "api"
                        )
                        break
                    print("Choose 1 for local data or 2 for the Kalshi API.")
            return ticker, selected_source
        if choice == "2":
            query = (
                search
                or input(
                    "Game/team name or date code (Enter to browse, q to quit): "
                ).strip()
            )
            if query.casefold() == "q":
                raise SystemExit(0)
            ticker = choose_local_game(data_dir, sport=sport, search=query)
            return ticker, source or "local"
        print("Choose 1, 2, or q.")


def choose_local_game(
    data_dir: Path, *, sport: str | None = None, search: str = ""
) -> str:
    """Search and page through local games, then choose an outcome market."""
    print("Reading local game catalog...", flush=True)
    games = list_local_games(data_dir, sport=sport)
    if games.empty:
        raise ValueError(
            "No local games match this sport. Use the ticker option in the menu to load from Kalshi."
        )
    text = games["event_ticker"] + " " + games["title"] + " " + games["outcomes"]

    def matching_games(query: str):
        mask = text.notna()
        for word in query.split():
            mask &= text.str.contains(word, case=False, regex=False)
        return games[mask].reset_index(drop=True)

    filtered = matching_games(search)
    if filtered.empty:
        print(
            f"No games match {search!r}. Showing all games; enter another search below."
        )
        filtered = games
    page = 0
    page_size = 20
    while True:
        start = page * page_size
        print(
            f"\nGames {start + 1}-{min(start + page_size, len(filtered))} of {len(filtered)}"
        )
        for index, row in filtered.iloc[start : start + page_size].iterrows():
            print(f"  {index + 1}. {row.event_ticker} | {row.outcomes or row.title}")
        choice = input(
            "Game number or team/name search (n/p page, / for all, q to quit): "
        ).strip()
        if choice.casefold() == "q":
            raise SystemExit(0)
        if choice.casefold() in ("n", "p"):
            page = max(
                0,
                min(
                    page + (1 if choice.casefold() == "n" else -1),
                    (len(filtered) - 1) // page_size,
                ),
            )
            continue
        if choice.startswith("/") or (choice and not choice.isdigit()):
            matches = matching_games(choice.removeprefix("/"))
            if matches.empty:
                print("No matching games.")
            else:
                filtered, page = matches, 0
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(filtered):
            event = filtered.iloc[int(choice) - 1]["event_ticker"]
            _, markets, _ = LocalGameStore(data_dir).resolve_markets(event)
            for index, market in enumerate(markets, 1):
                print(f"  {index}. {market.yes_sub_title or market.ticker}")
            while True:
                selection = input("Outcome number (q to quit): ").strip()
                if selection.casefold() == "q":
                    raise SystemExit(0)
                if selection.isdigit() and 1 <= int(selection) <= len(markets):
                    return markets[int(selection) - 1].ticker
                print("Enter one of the outcome numbers above.")
        else:
            print("Enter a game number or type a team/name to search.")


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
        if args.list_games:
            games = list_local_games(
                args.data_dir, sport=args.sport, search=args.search
            )
            print(
                games.to_string(index=False)
                if not games.empty
                else "No matching local games."
            )
            return 0
        if args.ticker:
            ticker, source = args.ticker, args.source or "api"
        else:
            ticker, source = choose_game(
                args.data_dir, source=args.source, sport=args.sport, search=args.search
            )
        print(f"Loading {ticker} from {source}...", flush=True)
        game = load_game_odds(
            ticker,
            target=args.target,
            start_time=args.start_time,
            end_time=args.end_time,
            source=source,
            data_dir=args.data_dir,
        )
        books, events = load_game_overlays(
            game,
            data_dir=args.data_dir,
            sportsbook_path=args.sportsbook,
            playbyplay_path=args.playbyplay,
            sportsbook_team=args.sportsbook_team,
            sportsbook_side=args.sportsbook_side,
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
            sportsbook_odds=books,
            pbp_events=events,
        )
    except (ValueError, OSError, duckdb.Error, httpx.HTTPError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("Game selection cancelled.") from None

    print(f"Event: {game.event_ticker}")
    print("Markets:")
    for market in game.markets:
        selected = "  <-- target" if market.ticker == game.target_ticker else ""
        label = market.yes_sub_title or market.ticker
        print(f"  {market.ticker} | {label}{selected}")
    print(f"Trades: {len(game.trades):,}")
    print(f"Plotted side: {game.target_label} ({game.target_side})")
    start_source = {
        "kalshi_milestone": "Kalshi API",
        "first_local_trade": "first stored trade; includes pregame",
        "provided": "provided",
    }[game.start_time_source]
    print(f"Start: {game.start_time.isoformat()} ({start_source})")
    print(f"Sportsbook quotes: {len(books):,} | Play-by-play events: {len(events):,}")

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
