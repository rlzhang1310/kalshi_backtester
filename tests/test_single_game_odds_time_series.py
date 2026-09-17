from __future__ import annotations

import threading
import urllib.request

import pandas as pd
import pytest

import visualize_game
from src.analysis.kalshi.single_game_odds_time_series import (
    GameOddsData,
    MAX_RESAMPLED_POINTS,
    build_game_odds_figure,
    compile_event_trades_to_target,
    load_game_odds,
)
from src.indexers.kalshi.client import KalshiClient
from src.indexers.kalshi.models import Market


def make_market(
    ticker: str,
    event_ticker: str,
    yes_sub_title: str,
    *,
    market_type: str = "binary",
) -> Market:
    return Market(
        ticker=ticker,
        event_ticker=event_ticker,
        market_type=market_type,
        title="Game winner",
        yes_sub_title=yes_sub_title,
        no_sub_title="",
        status="settled",
        yes_bid=None,
        yes_ask=None,
        no_bid=None,
        no_ask=None,
        last_price=None,
        volume=0,
        volume_24h=0,
        open_interest=0,
        result="",
        created_time=None,
        open_time=None,
        close_time=None,
    )


def raw_trade(
    trade_id: str,
    yes_price: str,
    created_time: str,
) -> dict:
    return {
        "trade_id": trade_id,
        "yes_price_dollars": yes_price,
        "no_price_dollars": str(DecimalOneMinus(yes_price)),
        "count_fp": "1.00",
        "created_time": created_time,
        "taker_side": "yes",
    }


class DecimalOneMinus:
    """Tiny formatting helper that avoids float artifacts in test payloads."""

    def __init__(self, value: str):
        self.value = value

    def __str__(self) -> str:
        return f"{1 - float(self.value):.4f}"


class FakeClient:
    def __init__(
        self,
        *,
        live_markets: dict[str, list[Market]] | None = None,
        historical_markets: dict[str, list[Market]] | None = None,
        live_trades: dict[str, list[dict]] | None = None,
        historical_trades: dict[str, list[dict]] | None = None,
        milestones: dict[str, list[dict]] | None = None,
    ):
        self.live_markets = live_markets or {}
        self.historical_markets = historical_markets or {}
        self.live_trades = live_trades or {}
        self.historical_trades = historical_trades or {}
        self.milestones = milestones
        self.calls: list[tuple[str, bool, str]] = []
        self.closed = False

    @property
    def all_markets(self) -> list[Market]:
        return [
            market
            for source in (self.live_markets, self.historical_markets)
            for markets in source.values()
            for market in markets
        ]

    def get_market(self, ticker: str, historical: bool = False) -> Market | None:
        self.calls.append(("market", historical, ticker))
        source = self.historical_markets if historical else self.live_markets
        return next(
            (
                market
                for markets in source.values()
                for market in markets
                if market.ticker == ticker
            ),
            None,
        )

    def get_event_markets(
        self,
        event_ticker: str,
        historical: bool = False,
        limit: int = 1000,
    ) -> list[Market]:
        del limit
        self.calls.append(("event", historical, event_ticker))
        source = self.historical_markets if historical else self.live_markets
        return source.get(event_ticker, [])

    def get_market_trades_data(
        self,
        ticker: str,
        limit: int = 1000,
        verbose: bool = True,
        min_ts: int | None = None,
        max_ts: int | None = None,
        historical: bool = False,
    ) -> list[dict]:
        del limit, verbose, min_ts, max_ts
        self.calls.append(("trades", historical, ticker))
        source = self.historical_trades if historical else self.live_trades
        return source.get(ticker, [])

    def get_event_milestones(
        self,
        event_ticker: str,
        limit: int = 500,
    ) -> list[dict]:
        del limit
        self.calls.append(("milestones", False, event_ticker))
        if self.milestones is not None:
            return self.milestones.get(event_ticker, [])
        return [
            {
                "id": f"milestone-{event_ticker}",
                "category": "Sports",
                "start_date": "1970-01-01T00:00:00Z",
                "primary_event_tickers": [event_ticker],
                "related_event_tickers": [event_ticker],
            }
        ]

    def close(self) -> None:
        self.closed = True


def test_load_event_combines_tiers_deduplicates_and_infers_target() -> None:
    event = "KXNFLGAME-25SEP07CINCLE"
    cle = make_market(f"{event}-CLE", event, "Cleveland")
    cin = make_market(f"{event}-CIN", event, "Cincinnati")
    duplicate = raw_trade("duplicate", "0.6000", "2025-09-07T17:00:00Z")
    client = FakeClient(
        live_markets={event: [cle]},
        historical_markets={event: [cin]},
        live_trades={cle.ticker: [duplicate]},
        historical_trades={
            cle.ticker: [duplicate],
            cin.ticker: [raw_trade("opponent", "0.4000", "2025-09-07T17:00:01Z")],
        },
    )

    game = load_game_odds(event.lower(), client=client)

    assert game.event_ticker == event
    assert game.target_ticker == cle.ticker
    assert game.target_side == "CLE"
    assert game.target_label == "Cleveland"
    assert len(game.trades) == 2
    assert game.compiled["target_raw_prob"].tolist() == pytest.approx([0.6, 0.6])
    assert client.calls.index(("event", False, event)) < client.calls.index(
        ("event", True, event)
    )
    assert client.calls.index(("trades", False, cle.ticker)) < client.calls.index(
        ("trades", True, cle.ticker)
    )
    assert not client.closed


def test_omitted_start_uses_kalshi_milestone_and_filters_pregame_trades() -> None:
    event = "KXNFLGAME-25SEP07CINCLE"
    cle = make_market(f"{event}-CLE", event, "Cleveland")
    cin = make_market(f"{event}-CIN", event, "Cincinnati")
    client = FakeClient(
        live_markets={event: [cle, cin]},
        live_trades={
            cle.ticker: [raw_trade("pregame", "0.5500", "2025-09-07T16:59:59Z")],
            cin.ticker: [raw_trade("in-game", "0.4000", "2025-09-07T17:00:01Z")],
        },
        milestones={
            event: [
                {
                    "id": "nfl-game",
                    "category": "Sports",
                    "start_date": "2025-09-07T17:00:00Z",
                    "primary_event_tickers": [event],
                    "related_event_tickers": [event],
                }
            ]
        },
    )

    game = load_game_odds(event, client=client)

    assert game.start_time == pd.Timestamp("2025-09-07T17:00:00Z")
    assert game.start_time_source == "kalshi_milestone"
    assert game.trades["trade_id"].tolist() == ["in-game"]
    assert ("milestones", False, event) in client.calls


def test_explicit_start_overrides_api_lookup() -> None:
    event = "KXNFLGAME-25SEP07CINCLE"
    cle = make_market(f"{event}-CLE", event, "Cleveland")
    cin = make_market(f"{event}-CIN", event, "Cincinnati")
    client = FakeClient(
        live_markets={event: [cle, cin]},
        live_trades={cle.ticker: [raw_trade("cle", "0.6000", "2025-09-07T18:00:00Z")]},
        milestones={event: []},
    )

    game = load_game_odds(
        event,
        start_time="2025-09-07T17:30:00Z",
        client=client,
    )

    assert game.start_time == pd.Timestamp("2025-09-07T17:30:00Z")
    assert game.start_time_source == "provided"
    assert not any(call[0] == "milestones" for call in client.calls)


def test_missing_kalshi_start_requests_explicit_override() -> None:
    event = "KXNFLGAME-25SEP07CINCLE"
    cle = make_market(f"{event}-CLE", event, "Cleveland")
    cin = make_market(f"{event}-CIN", event, "Cincinnati")
    client = FakeClient(
        live_markets={event: [cle, cin]},
        milestones={event: []},
    )

    with pytest.raises(ValueError, match="Pass start_time= or --start"):
        load_game_odds(event, client=client)


def test_child_ticker_selects_that_market_even_when_home_is_other_side() -> None:
    event = "KXNFLGAME-25SEP07CINCLE"
    cle = make_market(f"{event}-CLE", event, "Cleveland")
    cin = make_market(f"{event}-CIN", event, "Cincinnati")
    client = FakeClient(
        historical_markets={event: [cle, cin]},
        historical_trades={
            cle.ticker: [raw_trade("cle", "0.6000", "2025-09-07T17:00:00Z")],
            cin.ticker: [raw_trade("cin", "0.4000", "2025-09-07T17:00:01Z")],
        },
    )

    game = load_game_odds(cin.ticker.lower(), client=client)

    assert game.target_ticker == cin.ticker
    assert game.compiled["target_raw_prob"].tolist() == pytest.approx([0.4, 0.4])


def test_variable_length_suffix_and_exact_target_subtitle() -> None:
    event = "KXMLBGAME-26MAY251540NYYKC"
    nyy = make_market(f"{event}-NYY", event, "New York Yankees")
    kc = make_market(f"{event}-KC", event, "Kansas City Royals")
    trades = {
        nyy.ticker: [raw_trade("nyy", "0.5500", "2026-05-25T19:40:00Z")],
        kc.ticker: [raw_trade("kc", "0.4500", "2026-05-25T19:40:01Z")],
    }
    client = FakeClient(live_markets={event: [nyy, kc]}, live_trades=trades)

    inferred = load_game_odds(event, client=client)
    requested = load_game_odds(event, target="new york yankees", client=client)

    assert inferred.target_side == "KC"
    assert requested.target_side == "NYY"


def test_ambiguous_suffix_requires_target() -> None:
    event = "TEST-BA"
    a = make_market(f"{event}-A", event, "A")
    ba = make_market(f"{event}-BA", event, "BA")
    client = FakeClient(
        live_markets={event: [a, ba]},
        live_trades={
            a.ticker: [raw_trade("a", "0.5000", "2026-01-01T00:00:00Z")],
            ba.ticker: [raw_trade("ba", "0.5000", "2026-01-01T00:00:01Z")],
        },
    )

    with pytest.raises(ValueError, match="Could not infer"):
        load_game_odds(event, client=client)


def test_three_outcome_event_uses_only_selected_yes_market() -> None:
    event = "THREE-WAY"
    markets = [
        make_market(f"{event}-{suffix}", event, suffix)
        for suffix in ("ONE", "TWO", "THREE")
    ]
    client = FakeClient(
        live_markets={event: markets},
        live_trades={
            market.ticker: [raw_trade(market.ticker, "0.3000", "2026-01-01T00:00:00Z")]
            for market in markets
        },
    )
    game = load_game_odds(event, target="ONE", client=client)
    assert game.compiled["target_raw_prob"].tolist() == [0.3]
    assert {call[2] for call in client.calls if call[0] == "trades"} == {
        markets[0].ticker
    }


def test_compile_can_invert_opponent_when_target_has_no_trades() -> None:
    frame = pd.DataFrame(
        {
            "trade_id": ["one"],
            "timestamp": ["2026-01-01T00:00:00Z"],
            "ticker": ["EVENT-AWAY"],
            "yes_price": [0.35],
        }
    )

    compiled = compile_event_trades_to_target(
        frame,
        target_ticker="EVENT-HOME",
        target_side="HOME",
    )

    assert compiled.loc[0, "target_raw_prob"] == pytest.approx(0.65)
    assert compiled.loc[0, "taker_prob"] == pytest.approx(0.65 + 0.07 * 0.65 * 0.35)


def test_compile_requires_explicit_cents_and_validates_range() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": ["2026-01-01T00:00:00Z"],
            "ticker": ["EVENT-HOME"],
            "yes_price": [65],
        }
    )

    compiled = compile_event_trades_to_target(
        frame,
        target_ticker="EVENT-HOME",
        prices_are_cents=True,
    )
    assert compiled.loc[0, "target_raw_prob"] == pytest.approx(0.65)

    with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
        compile_event_trades_to_target(frame, target_ticker="EVENT-HOME")


def test_build_figure_matches_reference_format_and_resamples() -> None:
    compiled = pd.DataFrame(
        {
            "timestamp": [
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:02Z",
            ],
            "target_raw_prob": [0.4, 0.6],
            "taker_prob": [0.4168, 0.6168],
            "maker_prob": [0.4042, 0.6042],
        }
    )

    figure = build_game_odds_figure(
        compiled,
        event_ticker="EVENT",
        target_label="Home",
        resample_frequency="1s",
    )

    assert [trace.name for trace in figure.data] == [
        "Kalshi",
        "Kalshi taker",
        "Kalshi maker",
    ]
    assert len(figure.data[0].x) == 3
    assert figure.data[0].line.color == "#1f4aff"
    assert figure.data[1].line.dash == "dash"
    assert figure.data[2].line.dash == "dot"
    assert figure.layout.template.layout.plot_bgcolor == "white"
    assert tuple(figure.layout.yaxis.range) == (0, 1)
    assert figure.layout.yaxis.tickformat == ".0%"
    assert figure.layout.hovermode == "x unified"
    assert figure.layout.dragmode == "pan"
    assert figure.layout.margin.r == 360
    assert figure.layout.margin.b == 180
    assert figure.layout.xaxis.automargin is True
    assert figure.layout.yaxis.automargin is True
    assert "EVENT" in figure.layout.title.text


def test_raw_figure_is_step_line_and_can_hide_fees() -> None:
    compiled = pd.DataFrame(
        {
            "timestamp": ["2026-01-01T00:00:00Z"],
            "target_raw_prob": [0.5],
            "taker_prob": [0.5175],
            "maker_prob": [0.504375],
        }
    )
    figure = build_game_odds_figure(
        compiled,
        event_ticker="EVENT",
        target_label="Home",
        resample_frequency=None,
        show_fee_lines=False,
    )

    assert len(figure.data) == 1
    assert figure.data[0].line.shape == "hv"


def test_resample_point_guard_has_actionable_error() -> None:
    compiled = pd.DataFrame(
        {
            "timestamp": [
                "2026-01-01T00:00:00Z",
                pd.Timestamp("2026-01-01T00:00:00Z")
                + pd.Timedelta(seconds=MAX_RESAMPLED_POINTS),
            ],
            "target_raw_prob": [0.5, 0.5],
            "taker_prob": [0.5, 0.5],
            "maker_prob": [0.5, 0.5],
        }
    )

    with pytest.raises(ValueError, match="would create"):
        build_game_odds_figure(
            compiled,
            event_ticker="EVENT",
            target_label="Home",
            resample_frequency="1s",
        )


def test_cli_prints_chart_link_without_opening_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    compiled = pd.DataFrame(
        {
            "timestamp": [pd.Timestamp("2026-01-01T00:00:00Z")],
            "target_raw_prob": [0.5],
            "taker_prob": [0.5175],
            "maker_prob": [0.504375],
        }
    )
    market = make_market("EVENT-HOME", "EVENT", "Home")
    game = GameOddsData(
        event_ticker="EVENT",
        target_ticker=market.ticker,
        target_side="HOME",
        target_label="Home",
        start_time=pd.Timestamp("2026-01-01T00:00:00Z"),
        start_time_source="kalshi_milestone",
        markets=(market, make_market("EVENT-AWAY", "EVENT", "Away")),
        trades=compiled,
        compiled=compiled,
    )
    monkeypatch.setattr(visualize_game, "load_game_odds", lambda *args, **kwargs: game)
    output_path = tmp_path / "chart.html"

    assert (
        visualize_game.main(
            [
                "EVENT",
                "--no-show",
                "--no-resample",
                "--output-html",
                str(output_path),
            ]
        )
        == 0
    )
    assert output_path.exists()
    assert f"Chart file: {output_path.as_uri()}" in capsys.readouterr().out


def test_browser_chart_uses_printed_loopback_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fetched: list[bytes] = []
    fetch_threads: list[threading.Thread] = []

    def open_browser(url: str, new: int) -> bool:
        assert new == 2

        def fetch_chart() -> None:
            with urllib.request.urlopen(url, timeout=5) as response:
                fetched.append(response.read())

        thread = threading.Thread(target=fetch_chart)
        thread.start()
        fetch_threads.append(thread)
        return True

    monkeypatch.setattr(visualize_game.webbrowser, "open", open_browser)

    chart_url = visualize_game.open_chart_in_browser(b"<html>chart</html>")
    for thread in fetch_threads:
        thread.join(timeout=5)

    assert chart_url.startswith("http://127.0.0.1:")
    assert chart_url.endswith("/")
    assert f"Chart link: {chart_url}" in capsys.readouterr().out
    assert fetched == [b"<html>chart</html>"]


class FakeHttp:
    def __init__(self, responses: list[dict]):
        self.responses = iter(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, endpoint: str, *, params: dict) -> dict:
        self.calls.append((endpoint, params.copy()))
        return next(self.responses)


def test_client_raw_trade_pagination_and_historical_endpoint() -> None:
    http = FakeHttp(
        [
            {"trades": [{"trade_id": "one"}], "cursor": "next"},
            {"trades": [{"trade_id": "two"}], "cursor": ""},
        ]
    )
    client = KalshiClient.__new__(KalshiClient)
    client.http = http

    trades = client.get_market_trades_data(
        "EVENT-HOME",
        verbose=False,
        historical=True,
    )

    assert [trade["trade_id"] for trade in trades] == ["one", "two"]
    assert [call[0] for call in http.calls] == [
        "/historical/trades",
        "/historical/trades",
    ]
    assert "cursor" not in http.calls[0][1]
    assert http.calls[1][1]["cursor"] == "next"


def test_client_fetches_event_milestones_from_kalshi_api() -> None:
    http = FakeHttp(
        [
            {"milestones": [{"id": "one"}], "cursor": "next"},
            {"milestones": [{"id": "two"}], "cursor": ""},
        ]
    )
    client = KalshiClient.__new__(KalshiClient)
    client.http = http

    milestones = client.get_event_milestones("event")

    assert [milestone["id"] for milestone in milestones] == ["one", "two"]
    assert [call[0] for call in http.calls] == ["/milestones", "/milestones"]
    assert http.calls[0][1]["related_event_ticker"] == "EVENT"
    assert http.calls[0][1]["category"] == "Sports"
    assert http.calls[1][1]["cursor"] == "next"


def test_client_rejects_repeated_cursor() -> None:
    http = FakeHttp(
        [
            {"trades": [], "cursor": "same"},
            {"trades": [], "cursor": "same"},
        ]
    )
    client = KalshiClient.__new__(KalshiClient)
    client.http = http

    with pytest.raises(RuntimeError, match="repeated cursor"):
        client.get_market_trades_data("EVENT-HOME", verbose=False)
