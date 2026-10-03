from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import plotly.graph_objects as go

import volatility_app


def test_settled_api_game_loads_as_static_chart(monkeypatch, tmp_path: Path) -> None:
    game = SimpleNamespace(
        event_ticker="EVENT",
        target_label="Home",
        markets=(SimpleNamespace(status="settled"),),
        compiled=pd.DataFrame(),
    )
    loaded = {}

    def fake_load(ticker, **kwargs):
        loaded.update(ticker=ticker, **kwargs)
        return game

    class FakePollSession:
        def __init__(self, current, _build, refresh_seconds):
            self.game = current
            self.refresh_seconds = refresh_seconds
            self.last_poll = 1
            self.closed = False

        def close(self):
            self.closed = True

    class FakeChartServer:
        def __init__(self, html, session):
            self.html = html
            self.session = session
            self.url = "http://127.0.0.1:12345/"

        def close(self):
            pass

    monkeypatch.setattr(volatility_app, "load_game_odds", fake_load)
    monkeypatch.setattr(
        volatility_app, "load_game_overlays", lambda *_args, **_kwargs: (None, None)
    )
    built = {}

    def fake_build(*_args, **kwargs):
        built.update(kwargs)
        return go.Figure()

    monkeypatch.setattr(volatility_app, "build_game_odds_figure", fake_build)
    monkeypatch.setattr(volatility_app, "LiveGameSession", FakePollSession)
    monkeypatch.setattr(volatility_app, "ChartServer", FakeChartServer)

    active = volatility_app.load_chart(
        "Kalshi API", "event", "", "", "", "1s", True, False, tmp_path, 10, 7
    )
    assert loaded["ticker"] == "EVENT"
    assert loaded["source"] == "api"
    assert active["mode"] == "Kalshi API"
    assert built["volatility_cadence_seconds"] == 7
    assert active["volatility_cadence_seconds"] == 7
    assert not active["session"].available
    assert active["session"].snapshot(0)["connection"] == "settled"
    active["session"].close()
