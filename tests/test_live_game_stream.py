from __future__ import annotations

import asyncio
import base64
import json

import pandas as pd
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

import live_game
from src.analysis.kalshi import live_game_stream
from src.analysis.kalshi.live_game_stream import (
    WebSocketCredentials,
    WebSocketGameSession,
    normalize_ws_trade,
)
from src.analysis.kalshi.single_game_odds_time_series import (
    GameOddsData,
    build_game_odds_figure,
    compile_event_trades_to_target,
)
from src.indexers.kalshi.models import Market


EVENT = "KXNFLGAME-25SEP07CINCLE"


def make_game() -> GameOddsData:
    markets = tuple(
        Market.from_dict(
            {
                "ticker": f"{EVENT}-{side}",
                "event_ticker": EVENT,
                "status": "active",
                "yes_sub_title": name,
            }
        )
        for side, name in (("CLE", "Cleveland"), ("CIN", "Cincinnati"))
    )
    trades = pd.DataFrame(
        [
            {
                "trade_id": "initial",
                "event_ticker": EVENT,
                "ticker": markets[0].ticker,
                "timestamp": pd.Timestamp("2025-09-07T17:00:00Z"),
                "yes_price": 0.6,
                "no_price": 0.4,
                "count": 2.0,
            }
        ]
    )
    return GameOddsData(
        event_ticker=EVENT,
        target_ticker=markets[0].ticker,
        target_side="CLE",
        target_label="Cleveland",
        start_time=pd.Timestamp("2025-09-07T17:00:00Z"),
        start_time_source="provided",
        markets=markets,
        trades=trades,
        compiled=compile_event_trades_to_target(trades, markets[0].ticker, "CLE"),
    )


def trade_message(trade_id: str = "streamed") -> dict:
    return {
        "type": "trade",
        "msg": {
            "trade_id": trade_id,
            "market_ticker": f"{EVENT}-CIN",
            "yes_price_dollars": "0.3500",
            "no_price_dollars": "0.6500",
            "count_fp": "3.00",
            "ts_ms": int(pd.Timestamp("2025-09-07T17:00:01Z").timestamp() * 1000),
        },
    }


@pytest.mark.parametrize(
    "key",
    [
        ed25519.Ed25519PrivateKey.generate(),
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
    ],
)
def test_websocket_headers_sign_the_kalshi_handshake(key, monkeypatch) -> None:
    monkeypatch.setattr(live_game_stream.time, "time", lambda: 1_000)
    headers = WebSocketCredentials("key-id", key).headers()
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1000000"
    message = b"1000000GET/trade-api/ws/v2"
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    if isinstance(key, ed25519.Ed25519PrivateKey):
        key.public_key().verify(signature, message)
    else:
        key.public_key().verify(
            signature,
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )


def test_normalize_trade_uses_both_outcomes_and_filters_other_markets() -> None:
    game = make_game()
    record = normalize_ws_trade(trade_message(), game)
    assert record["ticker"] == f"{EVENT}-CIN"
    assert record["yes_price"] == 0.35
    assert record["count"] == 3
    compiled = compile_event_trades_to_target(pd.DataFrame([record]), game.target_ticker)
    assert compiled.target_raw_prob.tolist() == pytest.approx([0.65])
    wrong = trade_message()
    wrong["msg"]["market_ticker"] = "OTHER-GAME"
    assert normalize_ws_trade(wrong, game) is None


def test_game_session_subscribes_and_deduplicates_streamed_trades(monkeypatch) -> None:
    game = make_game()
    credentials = WebSocketCredentials("key-id", ed25519.Ed25519PrivateKey.generate())

    class FakeClient:
        def close(self):
            pass

    session = WebSocketGameSession(
        game,
        lambda current: build_game_odds_figure(
            current.compiled, event_ticker=EVENT, target_label="Cleveland"
        ),
        credentials,
        client=FakeClient(),
    )
    sent = []

    class FakeSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def send(self, payload):
            sent.append(json.loads(payload))

        async def recv(self):
            session.stop_event.set()
            return json.dumps(trade_message())

    monkeypatch.setattr(live_game_stream, "connect", lambda *args, **kwargs: FakeSocket())
    monkeypatch.setattr(session, "_reconcile", lambda: None)
    asyncio.run(session._stream())
    assert sent[0]["params"] == {
        "channels": ["trade"],
        "market_tickers": [f"{EVENT}-CLE", f"{EVENT}-CIN"],
    }
    assert game.trades.trade_id.tolist() == ["initial", "streamed"]
    assert game.compiled.target_raw_prob.tolist() == pytest.approx([0.6, 0.65])
    assert session.snapshot(0)["revision"] == 1
    session._add_records([normalize_ws_trade(trade_message(), game)])
    assert session.snapshot(1)["revision"] == 1


def test_stream_can_start_before_first_trade() -> None:
    game = make_game()
    game.trades = game.trades.iloc[0:0].copy()
    game.compiled = game.compiled.iloc[0:0].copy()

    class FakeClient:
        def close(self):
            pass

    session = WebSocketGameSession(
        game,
        lambda current: build_game_odds_figure(
            current.compiled, event_ticker=EVENT, target_label="Cleveland"
        ),
        WebSocketCredentials("key-id", ed25519.Ed25519PrivateKey.generate()),
        client=FakeClient(),
        initial_figure=object(),
    )
    record = normalize_ws_trade(trade_message(), game)
    session._add_records([record])
    assert game.trades.trade_id.tolist() == ["streamed"]
    assert game.compiled.target_raw_prob.tolist() == pytest.approx([0.65])
    assert session.snapshot(0)["revision"] == 1


def test_reconcile_backfills_trades_missed_by_the_socket() -> None:
    game = make_game()
    credentials = WebSocketCredentials("key-id", ed25519.Ed25519PrivateKey.generate())

    class FakeClient:
        def get_market(self, ticker, historical=False):
            return next(m for m in game.markets if m.ticker == ticker)

        def get_market_trades_data(self, ticker, *, historical=False, **_kwargs):
            if historical or ticker != game.target_ticker:
                return []
            return [
                {
                    "trade_id": "missed",
                    "created_time": "2025-09-07T17:00:03Z",
                    "yes_price_dollars": "0.7000",
                    "no_price_dollars": "0.3000",
                    "count_fp": "4.00",
                }
            ]

        def close(self):
            pass

    session = WebSocketGameSession(
        game,
        lambda current: build_game_odds_figure(
            current.compiled, event_ticker=EVENT, target_label="Cleveland"
        ),
        credentials,
        client=FakeClient(),
    )
    session._reconcile()
    assert game.trades.trade_id.tolist() == ["initial", "missed"]
    assert game.compiled.target_raw_prob.tolist() == pytest.approx([0.6, 0.7])
    assert session.snapshot(0)["revision"] == 1
    session._reconcile()
    assert session.snapshot(1)["revision"] == 1


def test_live_entrypoint_explains_missing_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(live_game, "ENV_PATH", tmp_path / ".env")
    with pytest.raises(SystemExit, match="KALSHI_API_KEY_ID"):
        live_game.main([EVENT])


def test_live_entrypoint_uses_project_env(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "KALSHI_API_KEY_ID=test-key\nKALSHI_PRIVATE_KEY_PATH=key.pem\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(live_game, "ENV_PATH", env_path)
    received = {}

    def fake_from_file(key_id, key_path):
        received["key_id"] = key_id
        received["key_path"] = key_path
        raise ValueError("stopped before loading game")

    monkeypatch.setattr(live_game.WebSocketCredentials, "from_file", fake_from_file)
    with pytest.raises(SystemExit, match="stopped before loading game"):
        live_game.main([EVENT])
    assert received == {"key_id": "test-key", "key_path": tmp_path / "key.pem"}
