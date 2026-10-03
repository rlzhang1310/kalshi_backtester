"""One-game Kalshi trade stream, ready to be owned by a multi-game UI later."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from src.analysis.kalshi.single_game_odds_time_series import (
    GameOddsData,
    compile_event_trades_to_target,
    game_is_unsettled,
    refresh_api_game,
)
from src.indexers.kalshi.client import KalshiClient


KALSHI_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
WS_SIGN_PATH = "/trade-api/ws/v2"
RECONCILE_SECONDS = 30
FLUSH_SECONDS = 0.5


class SubscriptionError(Exception):
    """Kalshi rejected the requested trade subscription."""


@dataclass(frozen=True)
class WebSocketCredentials:
    key_id: str
    private_key: ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey

    @classmethod
    def from_file(cls, key_id: str, key_path: str | Path) -> WebSocketCredentials:
        if not key_id.strip():
            raise ValueError("Kalshi API key ID is required")
        key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
        if not isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
            raise ValueError("Kalshi WebSocket key must be Ed25519 or RSA")
        return cls(key_id.strip(), key)

    def headers(self) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        message = (timestamp + "GET" + WS_SIGN_PATH).encode("utf-8")
        if isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(
                message,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }


def normalize_ws_trade(message: dict, game: GameOddsData) -> dict | None:
    """Convert one public trade message to the visualizer's trade schema."""
    if message.get("type") != "trade":
        return None
    data = message.get("msg") or {}
    ticker = str(data.get("market_ticker") or "").upper()
    allowed = (
        {market.ticker.upper() for market in game.markets}
        if len(game.markets) == 2
        else {game.target_ticker.upper()}
    )
    trade_id = str(data.get("trade_id") or "").strip()
    if ticker not in allowed or not trade_id:
        return None
    try:
        if data.get("ts_ms") is not None:
            timestamp = pd.Timestamp(int(data["ts_ms"]), unit="ms", tz="UTC")
        else:
            timestamp = pd.Timestamp(int(data["ts"]), unit="s", tz="UTC")
        yes_price = Decimal(str(data["yes_price_dollars"]))
        no_price = Decimal(str(data.get("no_price_dollars", 1 - yes_price)))
        count = Decimal(str(data["count_fp"]))
    except (KeyError, ValueError, TypeError, OverflowError, InvalidOperation):
        return None
    if not all(value.is_finite() for value in (yes_price, no_price, count)):
        return None
    if timestamp < game.start_time or not (0 <= yes_price <= 1 and 0 <= no_price <= 1):
        return None
    if count < 0:
        return None
    return {
        "trade_id": trade_id,
        "event_ticker": game.event_ticker,
        "ticker": ticker,
        "source_side": ticker.removeprefix(game.event_ticker.upper() + "-"),
        "timestamp": timestamp,
        "yes_price": float(yes_price),
        "no_price": float(no_price),
        "count": float(count),
        "taker_side": data.get("taker_side", ""),
    }


class WebSocketGameSession:
    """Own one game stream; expose versioned snapshots to the local web UI."""

    def __init__(
        self,
        game: GameOddsData,
        build_figure: Callable[[GameOddsData], object],
        credentials: WebSocketCredentials,
        *,
        client: KalshiClient | None = None,
        ws_url: str = KALSHI_WS_URL,
        initial_figure: object | None = None,
    ) -> None:
        self.game = game
        self.build_figure = build_figure
        self.credentials = credentials
        self.ws_url = ws_url
        self.figure = initial_figure if initial_figure is not None else build_figure(game)
        self.client = client or KalshiClient()
        self.revision = 0
        self.connection = "connecting"
        self.lock = Lock()
        self.stop_event = Event()
        self.thread: Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = Thread(target=self._run, name="kalshi-game-stream", daemon=True)
        self.thread.start()

    def snapshot(self, since: int) -> dict:
        with self.lock:
            result = {
                "revision": self.revision,
                "live": game_is_unsettled(self.game)
                and self.connection not in ("authentication failed", "subscription failed"),
                "connection": self.connection,
            }
            if self.revision > since:
                result["figure"] = json.loads(self.figure.to_json())
            return result

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.thread is None or not self.thread.is_alive():
            self.client.close()

    def _run(self) -> None:
        try:
            asyncio.run(self._stream())
        finally:
            self.client.close()

    def _add_records(self, records: list[dict]) -> None:
        if not records:
            return
        with self.lock:
            seen = set(self.game.trades["trade_id"])
            fresh = []
            for record in records:
                if record["trade_id"] not in seen:
                    fresh.append(record)
                    seen.add(record["trade_id"])
            if not fresh:
                return
            self.game.trades = (
                pd.concat([self.game.trades, pd.DataFrame.from_records(fresh)], ignore_index=True)
                .drop_duplicates("trade_id", keep="first")
                .sort_values(["timestamp", "trade_id"], kind="stable")
                .reset_index(drop=True)
            )
            self.game.compiled = compile_event_trades_to_target(
                self.game.trades, self.game.target_ticker, self.game.target_side
            )
            self.figure = self.build_figure(self.game)
            self.revision += 1

    def _reconcile(self) -> None:
        with self.lock:
            if game_is_unsettled(self.game) and refresh_api_game(self.game, self.client):
                self.figure = self.build_figure(self.game)
                self.revision += 1
            if not game_is_unsettled(self.game):
                self.connection = "settled"

    async def _stream(self) -> None:
        failures = 0
        while not self.stop_event.is_set() and game_is_unsettled(self.game):
            pending: list[dict] = []
            try:
                async with connect(
                    self.ws_url,
                    additional_headers=self.credentials.headers(),
                    ping_interval=20,
                    ping_timeout=20,
                ) as websocket:
                    tickers = (
                        [market.ticker for market in self.game.markets]
                        if len(self.game.markets) == 2
                        else [self.game.target_ticker]
                    )
                    await websocket.send(
                        json.dumps(
                            {
                                "id": 1,
                                "cmd": "subscribe",
                                "params": {"channels": ["trade"], "market_tickers": tickers},
                            }
                        )
                    )
                    # Subscribe before the REST backfill so in-flight trades are buffered.
                    await asyncio.to_thread(self._reconcile)
                    with self.lock:
                        if self.connection != "settled":
                            self.connection = "connected"
                    failures = 0
                    next_reconcile = time.monotonic() + RECONCILE_SECONDS
                    last_flush = time.monotonic()
                    while not self.stop_event.is_set() and game_is_unsettled(self.game):
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=FLUSH_SECONDS)
                            message = json.loads(raw)
                            if message.get("type") == "error":
                                raise SubscriptionError(
                                    str(message.get("msg", "WebSocket subscription error"))
                                )
                            record = normalize_ws_trade(message, self.game)
                            if record is not None:
                                pending.append(record)
                        except asyncio.TimeoutError:
                            pass
                        if pending and time.monotonic() - last_flush >= FLUSH_SECONDS:
                            self._add_records(pending)
                            pending = []
                            last_flush = time.monotonic()
                        if time.monotonic() >= next_reconcile:
                            if pending:
                                self._add_records(pending)
                                pending = []
                            await asyncio.to_thread(self._reconcile)
                            next_reconcile = time.monotonic() + RECONCILE_SECONDS
                    if pending:
                        self._add_records(pending)
            except SubscriptionError:
                if pending:
                    self._add_records(pending)
                with self.lock:
                    self.connection = "subscription failed"
                break
            except InvalidStatus as exc:
                if pending:
                    self._add_records(pending)
                if exc.response.status_code in (401, 403):
                    with self.lock:
                        self.connection = "authentication failed"
                    break
                if self.stop_event.is_set():
                    break
                with self.lock:
                    self.connection = "reconnecting"
                failures += 1
                await asyncio.sleep(min(2**failures, 30))
            except Exception:
                if pending:
                    self._add_records(pending)
                if self.stop_event.is_set():
                    break
                with self.lock:
                    self.connection = "reconnecting"
                failures += 1
                # A disconnected stream can miss trades; REST backfills on reconnect.
                await asyncio.sleep(min(2**failures, 30))
