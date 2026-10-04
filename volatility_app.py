"""One local UI for historical, polled, and streamed game charts.

Run with: python -m streamlit run volatility_app.py
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from live_game import load_websocket_credentials
from src.analysis.kalshi.chart_server import ChartServer
from src.analysis.kalshi.game_overlays import load_game_overlays
from src.analysis.kalshi.live_game_stream import WebSocketGameSession
from src.analysis.kalshi.local_game_data import DEFAULT_DATA_DIR, list_local_games
from src.analysis.kalshi.rolling_volatility import DEFAULT_CADENCE_SECONDS
from src.analysis.kalshi.rolling_volatility import rolling_volatility_payload
from src.analysis.kalshi.single_game_odds_time_series import (
    build_game_odds_figure,
    figure_display_config,
    game_is_unsettled,
    load_game_odds,
    volume_rebucket_script,
)
from src.analysis.kalshi.toggleable_polling import ToggleablePollingSession
from visualize_game import LiveGameSession


MODES = ("Historical", "Kalshi API", "Live WebSocket")


def stop_active() -> None:
    """Release the current Kalshi client and any streaming thread."""
    active = st.session_state.pop("game_active", None)
    if active:
        active["server"].close()
        if active["session"] is not None:
            active["session"].close()


def load_chart(
    mode: str,
    ticker: str,
    target: str,
    start: str,
    end: str,
    frequency: str,
    show_fees: bool,
    initial_dark: bool,
    data_dir: Path,
    refresh_seconds: int,
    volatility_cadence_seconds: int = DEFAULT_CADENCE_SECONDS,
) -> dict:
    if not ticker.strip():
        raise ValueError("Enter a game or market ticker.")
    if mode == "Live WebSocket" and end.strip():
        raise ValueError("End time is not available for WebSocket charts.")
    game = load_game_odds(
        ticker.strip().upper(),
        target=target.strip() or None,
        start_time=start.strip() or None,
        end_time=end.strip() or None,
        source="local" if mode == "Historical" else "api",
        data_dir=data_dir,
        allow_empty=mode != "Historical" and not end.strip(),
    )
    if mode == "Live WebSocket" and not game_is_unsettled(game):
        raise ValueError("This game is settled. Use Kalshi API mode for its chart.")
    books, events = (None, None)
    if mode in ("Historical", "Kalshi API"):
        books, events = load_game_overlays(game, data_dir=data_dir)

    def build(current):
        if current.compiled.empty:
            left = max(
                current.start_time,
                pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=5),
            )
            right = left + pd.Timedelta(minutes=30)
            figure = go.Figure()
            figure.add_trace(go.Scatter(x=[], y=[], name="Kalshi", mode="lines"))
            figure.add_trace(go.Bar(
                x=[], y=[], name="Volume", xaxis="x2", yaxis="y2",
                meta={"timestamps": [], "counts": [], "minimum_ms": 1000, "bucket_ms": 1000},
            ))
            figure.update_layout(
                title=f"{current.target_label} Implied Probability — waiting for first trade",
                height=750,
                xaxis={"range": [left.isoformat(), right.isoformat()], "minallowed": left.isoformat(),
                       "maxallowed": (right + pd.Timedelta(minutes=30)).isoformat()},
                xaxis2={"matches": "x", "anchor": "y2"},
                yaxis={"range": [0, 1], "domain": [0.26, 1], "tickformat": ".0%"},
                yaxis2={"domain": [0, 0.17], "anchor": "x2", "title": "Volume (contracts)"},
                meta={
                    "event_ticker": current.event_ticker,
                    "rolling_volatility": rolling_volatility_payload(
                        current.compiled, cadence_seconds=volatility_cadence_seconds
                    ),
                    "time_bounds": {"left": left.isoformat(), "right": right.isoformat()},
                },
            )
        else:
            figure = build_game_odds_figure(
                current.compiled,
                event_ticker=current.event_ticker,
                target_label=current.target_label,
                resample_frequency=None if frequency == "Raw trades" else frequency,
                volatility_cadence_seconds=volatility_cadence_seconds,
                show_fee_lines=show_fees,
                width=1250,
                height=750,
                sportsbook_odds=books,
                pbp_events=events,
            )
        figure.update_layout(width=None, autosize=True)
        if initial_dark:
            figure.update_layout(
                template="plotly_dark",
                paper_bgcolor="#111827",
                plot_bgcolor="#111827",
                font_color="#e5e7eb",
                legend={
                    "bgcolor": "rgba(30,41,59,0.95)",
                    "bordercolor": "#475569",
                    "font": {"color": "#f1f5f9"},
                    "title_font": {"color": "#f1f5f9"},
                },
            )
            figure.update_xaxes(gridcolor="#334155", zerolinecolor="#475569")
            figure.update_yaxes(gridcolor="#334155", zerolinecolor="#475569")
            figure.update_traces(line_color="#60a5fa", selector={"name": "Kalshi"})
            figure.update_traces(marker_color="#94a3b8", selector={"name": "Volume"})
        return figure

    figure = build(game)
    session = None
    server = None
    try:
        if mode == "Kalshi API":
            session = ToggleablePollingSession(
                LiveGameSession(game, build, refresh_seconds),
                allow_updates=not bool(end.strip()),
            )
        elif mode == "Live WebSocket":
            credentials = load_websocket_credentials()
            session = WebSocketGameSession(
                game, build, credentials, initial_figure=figure
            )
            session.start()
        theme_script = (
            Path(__file__).parent / "src" / "analysis" / "kalshi" / "game_theme_browser.js"
        ).read_text(encoding="utf-8").replace(
            "{initial_dark}", "true" if initial_dark else "false"
        )
        zoom_script = (
            Path(__file__).parent / "src" / "analysis" / "kalshi" / "game_zoom_browser.js"
        ).read_text(encoding="utf-8")
        volatility_script = (
            Path(__file__).parent / "src" / "analysis" / "kalshi"
            / "game_volatility_browser.js"
        ).read_text(encoding="utf-8").replace(
            "{live_clock}",
            "true" if mode != "Historical" and not end.strip() and game_is_unsettled(game) else "false",
        )
        scripts = [volume_rebucket_script(), theme_script, zoom_script, volatility_script]
        if session is not None:
            live_script = (
                Path(__file__).parent / "src" / "analysis" / "kalshi" / "game_live_browser.js"
            ).read_text(encoding="utf-8")
            scripts.append(live_script.replace("{refresh_ms}", "1000"))
        scripts.append(
            (
                Path(__file__).parent
                / "src"
                / "analysis"
                / "kalshi"
                / "game_fullscreen_browser.js"
            ).read_text(encoding="utf-8")
        )
        figure.update_layout(uirevision=game.event_ticker)
        html = figure.to_html(
            include_plotlyjs=True,
            full_html=True,
            config=figure_display_config(game.event_ticker, game.target_label),
            post_script=scripts,
        )
        if initial_dark:
            html = html.replace(
                "<head>",
                "<head><style>html,body{margin:0;background:#111827;color:#e5e7eb}</style>",
                1,
            )
        html = html.encode("utf-8")
        def metric_snapshot(at_ms: int | None) -> dict:
            current_session = (
                session.session if isinstance(session, ToggleablePollingSession) else session
            )
            if current_session is None:
                compiled = game.compiled
            else:
                with current_session.lock:
                    # Live sessions replace this frame, so retaining its
                    # reference is safe while the helper reads a subset.
                    compiled = current_session.game.compiled
            return rolling_volatility_payload(
                compiled,
                cadence_seconds=volatility_cadence_seconds,
                as_of=at_ms,
            )

        server = ChartServer(html, session, metric_provider=metric_snapshot)
    except Exception:
        if server is not None:
            server.close()
        if session is not None:
            session.close()
        raise
    return {
        "mode": mode,
        "game": game,
        "session": session,
        "has_end": bool(end.strip()),
        "volatility_cadence_seconds": volatility_cadence_seconds,
        "server": server,
        "url": server.url,
    }


def chart_area() -> None:
    active = st.session_state.get("game_active")
    if active is None:
        st.info("Select a mode and load a game to see its chart.")
        return
    game = active["game"]
    st.caption(f"{game.event_ticker} · {game.target_label}")
    st.link_button("Open chart in new tab", active["url"] + "?fullscreen=1")
    st.iframe(active["url"], height=1060)
    # Streamlit's theme picker can change without a Python rerun. Relay the
    # rendered app background to the chart iframe without remounting it.
    url = json.dumps(active["url"])
    st.html(
        "<script>" + r"""
        (() => {
          if (window.__gameChartThemeTimer) clearInterval(window.__gameChartThemeTimer);
          const url = URL_PLACEHOLDER;
          const origin = new URL(url).origin;
          let missing = 0;
          function relay() {
            const frame = [...document.querySelectorAll('iframe[src]')]
              .find(node => node.src === url);
            if (!frame) {
              if (++missing > 20) clearInterval(window.__gameChartThemeTimer);
              return;
            }
            missing = 0;
            const surfaces = [
              document.querySelector('[data-testid="stAppViewContainer"]'),
              document.querySelector('[data-testid="stApp"]'),
              document.body
            ];
            let rgb;
            for (const surface of surfaces) {
              if (!surface) continue;
              const values = getComputedStyle(surface).backgroundColor.match(/\d+(?:\.\d+)?/g);
              if (values?.length >= 3 && (values.length < 4 || Number(values[3]) > 0)) {
                rgb = values.map(Number);
                break;
              }
            }
            if (!rgb) return;
            const [r, g, b] = rgb;
            frame.contentWindow.postMessage(
              {type: 'game-chart-theme', dark: r * .2126 + g * .7152 + b * .0722 < 140},
              origin
            );
          }
          relay();
          window.__gameChartThemeTimer = setInterval(relay, 500);
        })();
        """.replace("URL_PLACEHOLDER", url) + "</script>",
        unsafe_allow_javascript=True,
    )


def main() -> None:
    st.set_page_config(page_title="Game chart visualizer", layout="wide")
    st.title("Game chart visualizer")
    mode = st.radio("Mode", MODES, horizontal=True)
    if st.session_state.get("game_mode") != mode:
        stop_active()
        st.session_state["game_mode"] = mode

    data_dir = DEFAULT_DATA_DIR
    if mode == "Historical":
        with st.expander("Browse stored games"):
            sport = st.text_input("Sport filter", placeholder="nfl, nba, mlb…")
            search = st.text_input("Search ticker or team")
            if st.button("Find games"):
                try:
                    with st.spinner("Reading local game catalog…"):
                        games = list_local_games(data_dir, sport=sport or None, search=search)
                    st.session_state["game_catalog"] = games
                except (ValueError, OSError, duckdb.Error) as exc:
                    st.error(str(exc))
            games = st.session_state.get("game_catalog")
            if isinstance(games, pd.DataFrame) and not games.empty:
                choices = games["event_ticker"].tolist()
                selected = st.selectbox("Stored game", choices, index=None)
                if selected and st.button("Use selected ticker"):
                    st.session_state["game_ticker"] = selected

    ticker = st.text_input(
        "Game event or market ticker",
        key="game_ticker",
        placeholder="KXATPCHALLENGERMATCH-26OCT01BASKRU",
    )
    with st.expander("Chart options"):
        target = st.text_input("Outcome (optional)", placeholder="Team code or market ticker")
        left, right = st.columns(2)
        with left:
            start = st.text_input("Start time (optional, ISO UTC)")
        with right:
            end = st.text_input("End time (optional, ISO UTC)") if mode != "Live WebSocket" else ""
        options = ["Raw trades", "1s", "3s", "5s", "1min", "5min"]
        frequency = st.selectbox(
            "Price line display interval (separate from volatility)", options, index=1
        )
        volatility_cadence_seconds = st.number_input(
            "Price metric sample cadence (seconds)",
            min_value=1,
            max_value=60,
            value=DEFAULT_CADENCE_SECONDS,
            step=1,
            help="Volatility and two-way movement metrics sample the last known price at this interval. Reload the chart to apply a change.",
        )
        show_fees = st.checkbox("Show maker/taker fee lines", value=True)
        refresh_seconds = (
            st.number_input("Poll every (seconds)", min_value=2, max_value=300, value=10)
            if mode == "Kalshi API"
            else 10
        )
    if mode == "Live WebSocket":
        st.caption("Uses Kalshi credentials from the project-root .env file.")

    start_col, stop_col = st.columns([1, 1])
    with start_col:
        if st.button("Start" if mode == "Live WebSocket" else "Load chart", type="primary"):
            stop_active()
            try:
                with st.spinner("Loading game and trade history…"):
                    st.session_state["game_active"] = load_chart(
                        mode, ticker, target, start, end, frequency, show_fees,
                        st.context.theme.type == "dark",
                        data_dir, int(refresh_seconds), int(volatility_cadence_seconds),
                    )
                    if mode == "Kalshi API":
                        st.session_state["api_updates"] = False
            except (ValueError, OSError, duckdb.Error, httpx.HTTPError) as exc:
                st.error(str(exc))
    with stop_col:
        if mode != "Historical" and st.button(
            "Stop" if mode == "Live WebSocket" else "Close chart",
            disabled="game_active" not in st.session_state,
        ):
            stop_active()
    active = st.session_state.get("game_active")
    if active and active.get(
        "volatility_cadence_seconds", DEFAULT_CADENCE_SECONDS
    ) != int(volatility_cadence_seconds):
        st.caption("Reload the chart to apply the new price metric cadence.")
    if mode == "Kalshi API" and active and active["mode"] == mode:
        controller = active["session"]
        controller.session.refresh_seconds = int(refresh_seconds)
        if not controller.available:
            st.session_state["api_updates"] = False
        enabled = st.toggle(
            "Update automatically",
            key="api_updates",
            disabled=not controller.available,
            help="Refreshes new trades at the selected interval when the event is unsettled.",
        )
        controller.set_enabled(enabled)
        if not controller.available:
            st.caption("Updates require an unsettled game without an end time.")
    chart_area()


if __name__ == "__main__":
    main()
