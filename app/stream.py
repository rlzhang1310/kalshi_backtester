"""One authenticated Kalshi socket shared by browser viewers and app orders."""

import asyncio
import json
import random
import time
import uuid

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from app.books import BookInvalid, BookRegistry


class KalshiFeed:
    def __init__(self, config, adapter, books: BookRegistry, store):
        self.config, self.adapter, self.books, self.store = config, adapter, books, store
        self.orders = None
        self.paper = None
        self.viewers: dict[str, tuple[set[str], float]] = {}
        self.connected = False
        self.last_error: str | None = None
        self.private_sid: int | None = None
        self.private_reconciled = False
        self.last_pong = 0.0
        self.generation = 0
        self.revision = 0
        self.on_change = None
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()

    @property
    def healthy(self) -> bool:
        return self.connected and time.monotonic() - self.last_pong < self.config.feed_health_timeout_seconds

    @property
    def order_stream_ready(self) -> bool:
        return self.healthy and self.private_sid is not None and self.private_reconciled

    def touch(self):
        self.revision += 1
        if self.on_change:
            self.on_change()

    def register(self, tickers: set[str]) -> str:
        if len(self.viewers) >= 32:
            raise ValueError("Too many browser streams")
        viewer_id = str(uuid.uuid4())
        self.viewers[viewer_id] = (tickers, time.monotonic() + 10)
        self.wake.set()
        return viewer_id

    def renew(self, viewer_id: str):
        if viewer_id in self.viewers:
            tickers, _ = self.viewers[viewer_id]
            self.viewers[viewer_id] = (tickers, time.monotonic() + 10)

    def release(self, viewer_id: str):
        self.viewers.pop(viewer_id, None)
        self.wake.set()

    def wanted(self) -> set[str]:
        now = time.monotonic()
        for key, (_, expires) in list(self.viewers.items()):
            if expires < now:
                self.viewers.pop(key, None)
        watched = set().union(*(tickers for tickers, _ in self.viewers.values())) if self.viewers else set()
        watched.update(record["ticker"] for record in self.store.unresolved())
        if self.paper:
            watched.update(self.paper.tickers)
        return watched

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        backoff = 1.0
        while True:
            if self.adapter.key is None:
                if self.last_error != "Kalshi WebSocket credentials are missing":
                    self.last_error = "Kalshi WebSocket credentials are missing"
                    self.touch()
                await asyncio.sleep(3)
                continue
            try:
                headers = self.adapter.sign_headers("GET", "/trade-api/ws/v2")
                async with connect(self.config.ws_url, additional_headers=headers, open_timeout=8,
                                   ping_interval=5, ping_timeout=5, max_queue=512) as websocket:
                    self.adapter.auth_failed = False
                    self.connected = True
                    self.last_error = None
                    self.last_pong = time.monotonic()
                    self.private_sid = None
                    self.private_reconciled = False
                    self.generation += 1
                    self.books.disconnect()
                    if self.paper:
                        self.paper.evaluate("refresh", time.perf_counter_ns(), time.perf_counter_ns())
                    self.touch()
                    backoff = 1.0
                    await self._connection(websocket, self.generation)
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                if exc.response.status_code in (401, 403):
                    self.adapter.auth_failed = True
                    self.last_error = "Kalshi WebSocket authentication failed"
                else:
                    self.last_error = f"Kalshi WebSocket HTTP {exc.response.status_code}"
            except BookInvalid as exc:
                self.last_error = f"Kalshi book synchronization failed: {exc}"
            except Exception:
                # Do not expose credentials, signatures, payloads, or raw server errors.
                self.last_error = "Kalshi WebSocket connection or message failed"
            self.connected = False
            self.private_sid = None
            self.private_reconciled = False
            self.books.disconnect()
            if self.paper:
                self.paper.evaluate("refresh", time.perf_counter_ns(), time.perf_counter_ns())
            self.touch()
            if self.orders:
                self.orders.recovering = True
            await asyncio.sleep(backoff + random.random() * min(1, backoff))
            backoff = min(30.0, backoff * 2)

    async def _connection(self, websocket, generation: int):
        next_id = 1
        book_sid: int | None = None
        book_seq: int | None = None
        initial_request_id: int | None = None
        requested: set[str] = set()
        private_request_id = next_id
        await websocket.send(json.dumps({"id": next_id, "cmd": "subscribe", "params": {"channels": ["user_orders"]}}))
        next_id += 1

        async def manage():
            nonlocal next_id, initial_request_id
            while True:
                desired = self.wanted()
                if book_sid is None and initial_request_id is None and desired:
                    initial_request_id = next_id
                    next_id += 1
                    requested.update(desired)
                    for market_ticker in desired:
                        self.books.get(market_ticker).state = "connecting"
                    self.books.touch()
                    await websocket.send(json.dumps({"id": initial_request_id, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta"], "market_tickers": sorted(desired),
                    }}))
                elif book_sid is not None:
                    added = sorted(desired - requested)
                    removed = sorted(requested - desired)
                    if added:
                        for market_ticker in added:
                            self.books.get(market_ticker).subscribe(generation, book_sid)
                        requested.update(added)
                        self.books.touch()
                        await websocket.send(json.dumps({"id": next_id, "cmd": "update_subscription", "params": {
                            "sid": book_sid, "market_tickers": added, "action": "add_markets",
                        }}))
                        next_id += 1
                    if removed:
                        requested.difference_update(removed)
                        for market_ticker in removed:
                            self.books.get(market_ticker).invalidate()
                        self.books.touch()
                        await websocket.send(json.dumps({"id": next_id, "cmd": "update_subscription", "params": {
                            "sid": book_sid, "market_tickers": removed, "action": "delete_markets",
                        }}))
                        next_id += 1
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                self.wake.clear()

        async def heartbeat():
            try:
                while True:
                    waiter = await websocket.ping()
                    await asyncio.wait_for(waiter, timeout=5)
                    self.last_pong = time.monotonic()
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                await websocket.close()
                raise

        async def reconcile_after_connect():
            await self.orders.reconcile_all()
            self.private_reconciled = True
            self.touch()

        manager = asyncio.create_task(manage())
        pinger = asyncio.create_task(heartbeat())
        recovery = asyncio.create_task(reconcile_after_connect())
        try:
            async for wire in websocket:
                received_ns = time.perf_counter_ns()
                event = json.loads(wire)
                event_type = event.get("type")
                sid = event.get("sid")
                message = event.get("msg")
                seq = event.get("seq")
                if sid == book_sid and isinstance(seq, int):
                    if book_seq is not None and seq != book_seq + 1:
                        raise BookInvalid("Book channel sequence gap")
                    book_seq = seq
                if event_type == "subscribed":
                    subscribed_sid = message["sid"]
                    if event.get("id") == private_request_id and message.get("channel") == "user_orders":
                        self.private_sid = subscribed_sid
                        self.touch()
                    elif event.get("id") == initial_request_id and message.get("channel") == "orderbook_delta":
                        book_sid = subscribed_sid
                        initial_request_id = None
                        for market_ticker in requested:
                            self.books.get(market_ticker).subscribe(generation, book_sid)
                        self.books.touch()
                        self.wake.set()
                elif event_type == "error":
                    code = message.get("code") if isinstance(message, dict) else None
                    raise BookInvalid(f"subscription error {code}" if isinstance(code, int)
                                      else "subscription error")
                elif event_type in {"orderbook_snapshot", "orderbook_delta"}:
                    if sid != book_sid:
                        continue
                    if not isinstance(message, dict) or not isinstance(message.get("market_ticker"), str):
                        raise BookInvalid("Book message missing market ticker")
                    market_ticker = message["market_ticker"]
                    if market_ticker not in requested:
                        continue  # A deletion may race with a queued book message.
                    book = self.books.get(market_ticker)
                    if event_type == "orderbook_snapshot":
                        book.snapshot(generation, sid, seq, message)
                    else:
                        book.delta(generation, sid, seq, message, shared_sequence=True)
                    applied_ns = time.perf_counter_ns()
                    book.last_received_ns = received_ns
                    if self.paper:
                        self.paper.evaluate(market_ticker, received_ns, applied_ns)
                    self.last_error = None
                    self.books.touch()
                elif event_type == "user_order" and sid == self.private_sid and isinstance(message, dict):
                    await self.orders.on_private_order(message)
        finally:
            for task in (manager, pinger, recovery):
                task.cancel()
            await asyncio.gather(manager, pinger, recovery, return_exceptions=True)
