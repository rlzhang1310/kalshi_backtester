"""Open a single Kalshi game in the authenticated live trade viewer."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import httpx
from dotenv import dotenv_values

from src.analysis.kalshi.live_game_stream import (
    WebSocketCredentials,
    WebSocketGameSession,
)
from src.analysis.kalshi.local_game_data import DEFAULT_DATA_DIR
from src.analysis.kalshi.single_game_odds_time_series import (
    build_game_odds_figure,
    figure_display_config,
    game_is_unsettled,
    load_game_odds,
    volume_rebucket_script,
)
from visualize_game import open_live_chart_in_browser

ENV_PATH = Path(__file__).with_name(".env")


def load_websocket_credentials() -> WebSocketCredentials:
    """Read the local project's Kalshi key without copying it into UI state."""
    settings = dotenv_values(ENV_PATH)
    key_id = (settings.get("KALSHI_API_KEY_ID") or "").strip()
    key_path = (settings.get("KALSHI_PRIVATE_KEY_PATH") or "").strip()
    if not key_id or not key_path:
        raise ValueError(
            f"Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH in {ENV_PATH}."
        )
    private_key_path = Path(key_path)
    if not private_key_path.is_absolute():
        private_key_path = ENV_PATH.parent / private_key_path
    return WebSocketCredentials.from_file(key_id, private_key_path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream one unsettled Kalshi game")
    parser.add_argument("ticker", help="Game event ticker or one of its market tickers")
    parser.add_argument("--target", help="Outcome ticker, suffix, or YES subtitle")
    parser.add_argument("--start", help="Override game start (ISO timestamp)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--frequency", default="1s", help="Odds plot interval")
    parser.add_argument("--no-fees", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        credentials = load_websocket_credentials()
        game = load_game_odds(
            args.ticker,
            target=args.target,
            start_time=args.start,
            source="api",
            data_dir=args.data_dir,
        )
        if not game_is_unsettled(game):
            raise ValueError("Game is settled; use visualize_game.py for its chart")

        def build_figure(current):
            return build_game_odds_figure(
                current.compiled,
                event_ticker=current.event_ticker,
                target_label=current.target_label,
                resample_frequency=args.frequency,
                show_fee_lines=not args.no_fees,
            )

        figure = build_figure(game)
        script = (
            Path(__file__).parent
            / "src"
            / "analysis"
            / "kalshi"
            / "game_live_browser.js"
        ).read_text(encoding="utf-8").replace("{refresh_ms}", "1000")
        config = figure_display_config(game.event_ticker, game.target_label)
        html = figure.to_html(
            include_plotlyjs=True,
            full_html=True,
            config=config,
            post_script=[volume_rebucket_script(), script],
        ).encode("utf-8")
        session = WebSocketGameSession(
            game, build_figure, credentials, initial_figure=figure
        )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        raise SystemExit(f"error: {exc}") from exc

    print(f"Streaming {game.event_ticker} | {game.target_label}")
    session.start()
    open_live_chart_in_browser(html, session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
