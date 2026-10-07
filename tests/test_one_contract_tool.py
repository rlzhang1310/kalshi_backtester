import asyncio
import base64
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Config
from app.books import BookInvalid, BookRegistry
from app.kalshi import ExchangeError, KalshiAdapter
from app.main import create_app
from app.markets import MarketService, normalize_book, on_grid
from app.models import OrderRequest, PairedOrderRequest
from app.orders import AppError, OrderService
from app.store import Store
from app.stream import KalshiFeed


class FakeAdapter:
    private_ready = True
    trade_scope_status = "allowed"
    last_success = 0
    key = None

    def __init__(self):
        self.creates = 0
        self.orders = {}
        self.lose_response = False
        self.market_status = "active"
        self.book = {"orderbook_fp": {"yes_dollars": [["0.46", "20.00"]], "no_dollars": [["0.51", "15.00"]]}}
        self.ranges = [{"start": "0", "end": "1", "step": "0.01"}]

    async def get_market(self, ticker):
        return {"ticker": ticker, "title": ticker, "status": self.market_status, "price_ranges": self.ranges}

    async def get_orderbook(self, ticker):
        return self.book

    async def create_order(self, order):
        self.creates += 1
        await asyncio.sleep(0.001)
        exchange_id = str(uuid.uuid4())
        self.orders[exchange_id] = {
            "order_id": exchange_id, "client_order_id": order["client_order_id"],
            "ticker": order["ticker"], "status": "resting",
            "fill_count_fp": "0.00", "remaining_count_fp": "1.00",
        }
        if self.lose_response:
            raise RuntimeError("response lost")
        return {"order_id": exchange_id, "fill_count": "0.00", "remaining_count": "1.00"}

    async def get_order(self, exchange_id):
        return self.orders[exchange_id]

    async def find_client_order(self, ticker, client_id):
        return next((o for o in self.orders.values() if o["ticker"] == ticker and o["client_order_id"] == client_id), None)

    async def cancel_order(self, record):
        raw = self.orders[record["exchange_order_id"]]
        raw.update(status="canceled", fill_count_fp="0.25", remaining_count_fp="0.00",
                   maker_fees_dollars="0.01", taker_fees_dollars="0")
        return {"reduced_by": "0.75"}

    async def get_fills(self, exchange_id):
        if self.orders[exchange_id]["fill_count_fp"] == "0.25":
            return [{"fill_id": "fill-1", "order_id": exchange_id, "count_fp": "0.25",
                     "yes_price_dollars": "0.46", "no_price_dollars": "0.54", "fee_cost": "0.01"}]
        return []

    async def close(self):
        pass


def service(tmp_path: Path, adapter=None):
    config = Config(environment="demo", trading_enabled=True, api_key_id="fake-key",
                    private_key_path="fake-path", db_path=tmp_path / "orders.sqlite3")
    adapter = adapter or FakeAdapter()
    store = Store(config)
    books = BookRegistry()
    book = books.get("TEST-MARKET")
    book.subscribe(1, 1)
    book.snapshot(1, 1, 1, {"yes_dollars_fp": adapter.book["orderbook_fp"]["yes_dollars"],
                            "no_dollars_fp": adapter.book["orderbook_fp"]["no_dollars"]})
    orders = OrderService(config, adapter, MarketService(adapter, config, books, lambda: True), store)
    orders.recovering = False
    return orders, adapter, store


def intent(request_id=None, outcome="yes", price="0.46"):
    return OrderRequest(request_id=request_id or uuid.uuid4(), ticker=" TEST-MARKET ",
                        outcome=outcome, price=price, mode="maker")


def pair_intent(target="same_market", relationship=None, other_outcome=None, pair_id=None,
                primary_price="0.46", second_price="0.51"):
    return PairedOrderRequest(pair_id=pair_id or uuid.uuid4(), primary_ticker="TEST-MARKET",
                              primary_outcome="yes", primary_price=primary_price,
                              second_target=target, other_ticker="TEST-OTHER" if target == "other_market" else None,
                              other_outcome=other_outcome, relationship=relationship,
                              second_price=second_price, mode="maker")


def test_book_normalization_and_empty_sides():
    quotes = normalize_book({"orderbook_fp": {"yes_dollars": [["0.46", "20"]], "no_dollars": [["0.51", "15"]]}})
    assert quotes["yes"] == {"bid_price": "0.46", "bid_quantity": "20", "ask_price": "0.49", "ask_quantity": "15", "spread": "0.03"}
    assert quotes["no"]["ask_price"] == "0.54"
    empty = normalize_book({"orderbook_fp": {"yes_dollars": [["0.46", "0"]], "no_dollars": []}})
    assert empty["yes"]["ask_price"] is None and empty["no"]["bid_price"] is None


def test_live_book_depth_delta_and_sequence_recovery():
    book = BookRegistry().get("TEST-MARKET")
    book.subscribe(1, 4)
    book.snapshot(1, 4, 7, {"yes_dollars_fp": [["0.45", "2"], ["0.46", "1"]],
                            "no_dollars_fp": [["0.50", "3"], ["0.51", "4"]]})
    view = book.view(True)
    assert view["quotes"]["yes"]["ask_price"] == "0.49"
    assert view["depth"]["yes"]["asks"][0] == {"price": "0.49", "quantity": "4"}
    book.delta(1, 4, 8, {"side": "yes", "price_dollars": "0.46", "delta_fp": "-1"})
    assert book.view(True)["quotes"]["yes"]["bid_price"] == "0.45"
    with pytest.raises(BookInvalid):
        book.delta(1, 4, 10, {"side": "yes", "price_dollars": "0.45", "delta_fp": "1"})
    book.invalidate()
    assert book.view(True)["stale"] is True
    book.subscribe(2, 9)
    with pytest.raises(BookInvalid):
        book.snapshot(1, 4, 11, {"yes_dollars_fp": [], "no_dollars_fp": []})
    book.snapshot(2, 9, 1, {"yes_dollars_fp": [], "no_dollars_fp": []})
    assert book.view(True)["stale"] is False


def test_empty_exchange_snapshot_with_omitted_sides_is_live():
    book = BookRegistry().get("TEST-MARKET")
    book.subscribe(1, 3)
    book.snapshot(1, 3, 1, {"market_ticker": "TEST-MARKET", "market_id": "market-id"})
    view = book.view(True)
    assert view["book_state"] == "live"
    assert view["quotes"]["yes"]["bid_price"] is None
    assert view["depth"]["no"] == {"bids": [], "asks": []}
    book.delta(1, 3, 2, {"side": "yes", "price_dollars": "0.25", "delta_fp": "3"})
    assert book.view(True)["quotes"]["yes"]["bid_price"] == "0.25"


def test_shared_book_subscription_sequences_are_global(tmp_path):
    config = Config(environment="demo", trading_enabled=False, db_path=tmp_path / "shared.sqlite3")
    adapter = FakeAdapter()
    store = Store(config)
    books = BookRegistry()
    feed = KalshiFeed(config, adapter, books, store)

    class Orders:
        recovering = False

        async def reconcile_all(self):
            pass

    class Socket:
        def __init__(self):
            self.events = asyncio.Queue()

        async def send(self, wire):
            command = json.loads(wire)
            if command["cmd"] == "subscribe" and command["params"]["channels"] == ["user_orders"]:
                await self.events.put({"type": "subscribed", "id": command["id"],
                                       "msg": {"channel": "user_orders", "sid": 1}})
            elif command["cmd"] == "subscribe":
                assert command["params"]["market_tickers"] == ["GAME-A"]
                await self.events.put({"type": "subscribed", "id": command["id"],
                                       "msg": {"channel": "orderbook_delta", "sid": 2}})
                await self.events.put({"type": "orderbook_snapshot", "sid": 2, "seq": 1,
                                       "msg": {"market_ticker": "GAME-A"}})
            elif command["cmd"] == "update_subscription":
                assert command["params"]["action"] == "add_markets"
                assert command["params"]["market_tickers"] == ["GAME-B"]
                await self.events.put({"type": "ok", "id": command["id"], "sid": 2, "seq": 2,
                                       "msg": {"market_tickers": ["GAME-A", "GAME-B"]}})
                await self.events.put({"type": "orderbook_snapshot", "sid": 2, "seq": 3,
                                       "msg": {"market_ticker": "GAME-B"}})
                await self.events.put({"type": "orderbook_delta", "sid": 2, "seq": 4,
                                       "msg": {"market_ticker": "GAME-A", "side": "yes",
                                               "price_dollars": "0.4", "delta_fp": "1"}})
                await self.events.put({"type": "orderbook_delta", "sid": 2, "seq": 5,
                                       "msg": {"market_ticker": "GAME-B", "side": "no",
                                               "price_dollars": "0.5", "delta_fp": "2"}})

        async def ping(self):
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

        async def close(self):
            pass

        async def __aiter__(self):
            while True:
                yield json.dumps(await self.events.get())

    async def run():
        feed.orders = Orders()
        feed.connected = True
        feed.last_pong = time.monotonic()
        feed.register({"GAME-A"})
        task = asyncio.create_task(feed._connection(Socket(), 1))
        try:
            for _ in range(100):
                if books.get("GAME-A").state == "live":
                    break
                await asyncio.sleep(0.01)
            feed.register({"GAME-B"})
            for _ in range(100):
                if books.get("GAME-B").seq == 5:
                    break
                await asyncio.sleep(0.01)
            assert feed.order_stream_ready
            assert books.get("GAME-A").view(True)["quotes"]["yes"]["bid_price"] == "0.4"
            assert books.get("GAME-B").view(True)["quotes"]["no"]["bid_price"] == "0.5"
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    store.close()


def test_private_update_before_create_response(tmp_path):
    orders, adapter, store = service(tmp_path)
    request = intent()

    async def create_with_private_update(record):
        adapter.creates += 1
        await orders.on_private_order({"client_order_id": record["client_order_id"],
                                       "order_id": "early-id", "ticker": record["ticker"],
                                       "status": "resting", "fill_count_fp": "0", "remaining_count_fp": "1"})
        adapter.orders["early-id"] = {"client_order_id": record["client_order_id"],
                                      "order_id": "early-id", "ticker": record["ticker"],
                                      "status": "resting", "fill_count_fp": "0", "remaining_count_fp": "1"}
        return {"order_id": "early-id"}

    adapter.create_order = create_with_private_update
    result, _ = asyncio.run(orders.submit(request))
    assert result["status"] == "resting" and result["exchange_order_id"] == "early-id"
    assert adapter.creates == 1
    store.close()


def test_request_restrictions_and_subcent_grid():
    with pytest.raises(ValidationError):
        OrderRequest(request_id=uuid.uuid4(), ticker="X-1", outcome="yes", price="0.46", mode="maker", quantity=2)
    for bad in ("NaN", "Infinity", "0", "1", "0.12345"):
        with pytest.raises(ValidationError):
            intent(price=bad)
    assert on_grid(__import__("decimal").Decimal("0.465"), [{"start": "0", "end": "1", "step": "0.005"}])
    assert not on_grid(__import__("decimal").Decimal("0.463"), [{"start": "0", "end": "1", "step": "0.005"}])


def test_no_exchange_translation_and_signature(tmp_path):
    captured = {}
    adapter = object.__new__(KalshiAdapter)

    async def capture(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"order_id": "exchange-id"}

    adapter._request = capture
    asyncio.run(adapter.create_order({"ticker": "TEST-MARKET", "client_order_id": str(uuid.uuid4()),
                                      "outcome": "no", "limit_price": "0.46", "expires_unix": 1800000000}))
    payload = captured["json"]
    assert (captured["method"], captured["path"], payload["side"], payload["price"], payload["count"]) == (
        "POST", "/portfolio/events/orders", "ask", "0.54", "1")
    assert payload["post_only"] is True and payload["exchange_index"] == -1

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "key.pem"
    path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    config = Config(api_key_id="test-key", private_key_path=str(path), db_path=tmp_path / "other.db")
    signer = KalshiAdapter(config)
    headers = signer._headers("DELETE", "/portfolio/events/orders/order-id")
    private.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
                                f"{headers['KALSHI-ACCESS-TIMESTAMP']}DELETE/trade-api/v2/portfolio/events/orders/order-id".encode(),
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())
    asyncio.run(signer.close())


@pytest.mark.parametrize(("scopes", "subaccount", "expected"), [
    (["read"], None, "read_only"),
    (["read", "write::trade"], None, "allowed"),
    (["read", "write"], 0, "allowed"),
    (["read", "write"], 3, "wrong_subaccount"),
])
def test_trade_scope_check(scopes, subaccount, expected):
    adapter = object.__new__(KalshiAdapter)
    adapter.key = object()
    adapter.auth_failed = False
    adapter.config = SimpleNamespace(api_key_id="configured-key")
    adapter.trade_scope_status = "unknown"

    async def fake_request(method, path, **kwargs):
        assert (method, path, kwargs["private"]) == ("GET", "/api_keys", True)
        return {"api_keys": [{"api_key_id": "another-key", "scopes": ["write"]},
                             {"api_key_id": "configured-key", "scopes": scopes, "subaccount": subaccount}]}

    adapter._request = fake_request
    assert asyncio.run(adapter.check_trade_scope()) == expected


def test_write_permission_denial_does_not_disable_private_auth():
    adapter = object.__new__(KalshiAdapter)
    adapter.config = SimpleNamespace(api_key_id="configured-key")
    adapter.key = object()
    adapter.auth_failed = False
    adapter.read_cooldown_until = 0
    adapter._headers = lambda method, path: {}
    adapter.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(403, json={"error": {"code": "scope_denied"}})
    ), base_url="https://example.test")

    async def run():
        with pytest.raises(ExchangeError) as exc:
            await adapter._request("POST", "/portfolio/events/orders", private=True, json={})
        await adapter.close()
        return exc.value

    error = asyncio.run(run())
    assert (error.status, error.code) == (403, "scope_denied")
    assert adapter.private_ready


def test_read_only_key_blocks_order_before_exchange(tmp_path):
    config = Config(environment="demo", trading_enabled=True, api_key_id="fake-key",
                    private_key_path="fake-path", db_path=tmp_path / "orders.sqlite3")
    adapter = FakeAdapter()
    adapter.trade_scope_status = "read_only"
    with TestClient(create_app(config, adapter), base_url="http://localhost") as client:
        status = client.get("/api/status").json()
        assert status["trade_scope_status"] == "read_only"
        response = client.post("/api/orders", json={"request_id": str(uuid.uuid4()),
                               "ticker": "TEST-MARKET", "outcome": "yes", "price": "0.46", "mode": "maker"},
                               headers={"Origin": "http://localhost", "X-Request-Token": status["request_token"]})
        assert response.status_code == 403 and response.json()["code"] == "trade_scope_missing"
    assert adapter.creates == 0


def test_identical_concurrent_requests_create_once(tmp_path):
    orders, adapter, store = service(tmp_path)
    request = intent()

    async def run():
        return await asyncio.gather(*(orders.submit(request) for _ in range(10)))

    results = asyncio.run(run())
    assert adapter.creates == 1
    assert {record["request_id"] for record, _ in results} == {str(request.request_id)}
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit(intent(request.request_id, outcome="no")))
    assert exc.value.code == "request_conflict"
    store.close()


def test_market_slot_crossing_and_no_translation(tmp_path):
    orders, adapter, store = service(tmp_path)
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit(intent(price="0.49")))
    assert exc.value.code == "would_take_liquidity" and adapter.creates == 0
    record, _ = asyncio.run(orders.submit(intent(outcome="no")))
    assert record["status"] == "resting"
    yes, _ = asyncio.run(orders.submit(intent()))
    assert yes["status"] == "resting"
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit(intent()))
    assert exc.value.code == "active_market_order" and adapter.creates == 2
    store.close()


def test_closed_off_grid_and_empty_book(tmp_path):
    orders, adapter, store = service(tmp_path)
    adapter.market_status = "closed"
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit(intent()))
    assert exc.value.code == "market_unavailable"
    adapter.market_status = "active"
    orders.markets.cache.clear()  # Simulate the market cache expiring before the next post.
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit(intent(price="0.465")))
    assert exc.value.code == "off_grid"
    adapter.book = {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
    live = orders.markets.books.get("TEST-MARKET")
    live.subscribe(1, 1)
    live.snapshot(1, 1, 2, {"yes_dollars_fp": [], "no_dollars_fp": []})
    record, _ = asyncio.run(orders.submit(intent()))
    assert record["status"] == "resting"
    store.close()


def test_lost_response_recovery_cancel_fractional_and_restart(tmp_path):
    orders, adapter, store = service(tmp_path)
    adapter.lose_response = True
    request = intent()
    unknown, _ = asyncio.run(orders.submit(request))
    assert unknown["status"] == "unknown" and adapter.creates == 1
    store.close()
    recovered_service, _, reopened = service(tmp_path, adapter)
    asyncio.run(recovered_service.startup_recovery())
    recovered = reopened.get(str(request.request_id))
    assert recovered["status"] == "resting" and adapter.creates == 1
    canceled = asyncio.run(recovered_service.cancel(str(request.request_id)))
    assert canceled["status"] == "cancel_pending"
    canceled = asyncio.run(recovered_service.reconcile(canceled))
    assert (canceled["status"], canceled["filled_quantity"], canceled["remaining_quantity"], canceled["canceled_quantity"]) == ("canceled", "0.25", "0", "0.75")
    assert canceled["average_fill_price"] == "0.46" and canceled["actual_fees"] == "0.01"
    reopened.close()


def test_warm_submit_returns_create_ack_without_extra_rest_reads(tmp_path):
    orders, adapter, store = service(tmp_path)
    asyncio.run(orders.markets.summary("TEST-MARKET"))

    async def unexpected_read(*_args):
        raise AssertionError("A warm submit should not wait for another REST read")

    adapter.get_market = unexpected_read
    adapter.get_order = unexpected_read
    request = intent()
    result, created = asyncio.run(orders.submit(request))
    assert created and result["status"] == "resting"
    assert result["exchange_order_id"] and adapter.creates == 1
    store.close()


def test_cancel_returns_pending_without_waiting_for_order_read(tmp_path):
    orders, adapter, store = service(tmp_path)
    request = intent()
    asyncio.run(orders.submit(request))

    async def unexpected_read(*_args):
        raise AssertionError("Cancel response should not wait for an order lookup")

    adapter.get_order = unexpected_read
    result = asyncio.run(orders.cancel(str(request.request_id)))
    assert result["status"] == "cancel_pending"
    assert result["canceled_quantity"] is None
    store.close()


def test_ack_cannot_regress_private_fill(tmp_path):
    orders, adapter, store = service(tmp_path)
    request = intent()

    async def create_with_fill(record):
        await orders.on_private_order({"client_order_id": record["client_order_id"],
                                       "order_id": "filled-early", "ticker": record["ticker"],
                                       "status": "executed", "fill_count_fp": "1",
                                       "remaining_count_fp": "0"})
        return {"order_id": "filled-early", "client_order_id": record["client_order_id"],
                "fill_count": "0", "remaining_count": "1"}

    adapter.create_order = create_with_fill
    result, _ = asyncio.run(orders.submit(request))
    assert result["status"] == "filled" and result["filled_quantity"] == "1"
    store.close()


def test_sse_emits_connecting_state_without_waiting_for_metadata(tmp_path):
    config = Config(environment="demo", trading_enabled=False, db_path=tmp_path / "stream.sqlite3")
    adapter = FakeAdapter()
    waiting = asyncio.Event()

    async def slow_market(ticker):
        await waiting.wait()
        return {"ticker": ticker, "title": ticker, "status": "active", "price_ranges": adapter.ranges}

    adapter.get_market = slow_market
    app = create_app(config, adapter)

    class Request:
        async def is_disconnected(self):
            return False

    async def run():
        async with app.router.lifespan_context(app):
            route = next(route for route in app.routes if getattr(route, "path", None) == "/api/events")
            response = await route.endpoint(Request(), tickers="TEST-MARKET")
            first = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=1)
            assert '"book_state": "connecting"' in first
            assert '"status": "unavailable"' in first
            waiting.set()
            await response.body_iterator.aclose()

    asyncio.run(run())


def test_read_only_http_contract_and_same_origin(tmp_path):
    config = Config(environment="demo", trading_enabled=False, db_path=tmp_path / "orders.sqlite3")
    adapter = FakeAdapter()
    with TestClient(create_app(config, adapter), base_url="http://localhost") as client:
        assert client.get("/").status_code == 200
        status = client.get("/api/status").json()
        assert status["trading_enabled"] is False and status["environment"] == "demo"
        summary = client.get("/api/markets/TEST-MARKET/summary").json()
        assert summary["book_state"] == "connecting" and summary["tradable"] is False
        body = {"request_id": str(uuid.uuid4()), "ticker": "TEST-MARKET", "outcome": "yes", "price": "0.46", "mode": "maker"}
        assert client.post("/api/orders", json=body).json()["code"] == "invalid_origin"
        response = client.post("/api/orders", json=body, headers={"Origin": "http://localhost", "X-Request-Token": status["request_token"]})
        assert response.status_code == 403 and response.json()["code"] == "trading_disabled"
        invalid = {**body, "quantity": "2"}
        response = client.post("/api/orders", json=invalid, headers={"Origin": "http://localhost", "X-Request-Token": status["request_token"]})
        assert response.status_code == 422 and response.json()["code"] == "invalid_request"
        assert adapter.creates == 0


def test_http_post_returns_exchange_ack_and_timing(tmp_path):
    config = Config(environment="demo", trading_enabled=True, api_key_id="fake-key",
                    private_key_path="fake-path", db_path=tmp_path / "http-orders.sqlite3")
    adapter = FakeAdapter()
    app = create_app(config, adapter)
    with TestClient(app, base_url="http://localhost") as client:
        feed = app.state.feed
        feed.connected = True
        feed.last_pong = time.monotonic()
        feed.private_sid = 1
        feed.private_reconciled = True
        book = app.state.books.get("TEST-MARKET")
        book.subscribe(1, 2)
        book.snapshot(1, 2, 1, {"yes_dollars_fp": [["0.46", "20"]],
                                 "no_dollars_fp": [["0.51", "15"]]})
        assert client.get("/api/markets/TEST-MARKET/summary").status_code == 200
        token = client.get("/api/status").json()["request_token"]
        response = client.post("/api/orders", json={"request_id": str(uuid.uuid4()),
                               "ticker": "TEST-MARKET", "outcome": "yes", "price": "0.46",
                               "mode": "maker"},
                               headers={"Origin": "http://localhost", "X-Request-Token": token})
        assert response.status_code == 201
        assert response.json()["status"] == "resting"
        assert "validation;dur=" in response.headers["Server-Timing"]
        assert "exchange;dur=" in response.headers["Server-Timing"]
        assert adapter.creates == 1


def test_pair_outcome_mapping_and_invalid_relationship():
    for other_outcome in ("yes", "no"):
        for relation in ("same_outcome", "opposite_outcomes"):
            request = pair_intent("other_market", relation, other_outcome)
            for primary in ("yes", "no"):
                request = request.model_copy(update={"primary_outcome": primary})
                first, second = request.legs()
                assert (first.ticker, first.outcome) == ("TEST-MARKET", primary)
                expected = ("no" if other_outcome == "yes" else "yes") if relation == "same_outcome" else other_outcome
                assert (second.ticker, second.outcome) == ("TEST-OTHER", expected)
    assert [(leg.ticker, leg.outcome) for leg in pair_intent().legs()] == [
        ("TEST-MARKET", "yes"), ("TEST-MARKET", "no")]
    with pytest.raises(ValueError):
        pair_intent("other_market", None, "yes").legs()


def test_pair_dispatches_concurrently_and_is_idempotent(tmp_path):
    orders, adapter, store = service(tmp_path)
    entered = []
    both = asyncio.Event()
    original = adapter.create_order

    async def barrier(record):
        entered.append(record["request_id"])
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)
        return await original(record)

    adapter.create_order = barrier
    request = pair_intent()

    async def run():
        return await asyncio.gather(*(orders.submit_pair(request) for _ in range(3)))

    results = asyncio.run(run())
    assert len(entered) == 2 and adapter.creates == 2
    assert all(len(rows) == 2 for rows, _ in results)
    assert {row["status"] for row in store.pair(str(request.pair_id))} == {"resting"}
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit_pair(pair_intent(pair_id=request.pair_id, second_price="0.50")))
    assert exc.value.code == "request_conflict"
    store.close()


def test_cross_market_pair_validates_both_before_dispatch(tmp_path):
    orders, adapter, store = service(tmp_path)
    book = orders.markets.books.get("TEST-OTHER")
    book.subscribe(1, 2)
    book.snapshot(1, 2, 1, {"yes_dollars_fp": [["0.46", "5"]], "no_dollars_fp": [["0.51", "5"]]})
    same = pair_intent("other_market", "same_outcome", "yes")
    rows, created = asyncio.run(orders.submit_pair(same))
    assert created and [(row["ticker"], row["outcome"]) for row in rows] == [
        ("TEST-MARKET", "yes"), ("TEST-OTHER", "no")]
    assert adapter.creates == 2
    opposite = pair_intent("other_market", "opposite_outcomes", "yes", second_price="0.46")
    with pytest.raises(AppError) as exc:
        asyncio.run(orders.submit_pair(opposite))
    assert exc.value.code == "active_market_order" and adapter.creates == 2
    store.close()


def test_pair_invalid_spread_tick_or_crossing_sends_nothing(tmp_path):
    orders, adapter, store = service(tmp_path)
    cases = [(pair_intent(primary_price="0.48", second_price="0.53"), "invalid_spread"),
             (pair_intent(second_price="0.54"), "would_take_liquidity"),
             (pair_intent(primary_price="0.465"), "off_grid")]
    for request, code in cases:
        with pytest.raises(AppError) as exc:
            asyncio.run(orders.submit_pair(request))
        assert exc.value.code == code
    assert adapter.creates == 0 and store.list() == []
    adapter.ranges = [{"start": "0", "end": "1", "step": "0.005"}]
    orders.markets.cache.clear()
    rows, _ = asyncio.run(orders.submit_pair(pair_intent(primary_price="0.465", second_price="0.505")))
    assert [row["limit_price"] for row in rows] == ["0.465", "0.505"]
    assert adapter.creates == 2
    store.close()


def test_pair_one_leg_fill_and_cancel_race(tmp_path):
    orders, adapter, store = service(tmp_path)
    original_create = adapter.create_order

    async def create_with_fill(record):
        ack = await original_create(record)
        if record["outcome"] == "yes":
            adapter.orders[ack["order_id"]].update(status="executed", fill_count_fp="1", remaining_count_fp="0")
            ack.update(fill_count="1", remaining_count="0")
        return ack

    adapter.create_order = create_with_fill
    request = pair_intent()
    rows, _ = asyncio.run(orders.submit_pair(request))
    assert [row["status"] for row in rows] == ["filled", "resting"]
    canceled = asyncio.run(orders.cancel_pair(str(request.pair_id)))
    assert [row["status"] for row in canceled] == ["filled", "cancel_pending"]
    store.close()

    raced, adapter, reopened = service(tmp_path / "race")
    request = pair_intent()
    asyncio.run(raced.submit_pair(request))
    original_cancel = adapter.cancel_order

    async def fill_during_cancel(record):
        if record["outcome"] == "yes":
            await raced.on_private_order({"ticker": record["ticker"], "client_order_id": record["client_order_id"],
                                          "order_id": record["exchange_order_id"], "status": "executed",
                                          "fill_count_fp": "1", "remaining_count_fp": "0"})
            return {"reduced_by": "0"}
        return await original_cancel(record)

    adapter.cancel_order = fill_during_cancel
    rows = asyncio.run(raced.cancel_pair(str(request.pair_id)))
    assert [row["status"] for row in rows] == ["filled", "cancel_pending"]
    reopened.close()


def test_pair_partial_reject_cancel_and_restart(tmp_path):
    orders, adapter, store = service(tmp_path)
    original = adapter.create_order

    async def one_reject(record):
        if record["outcome"] == "no":
            raise ExchangeError(422, "invalid_order")
        return await original(record)

    adapter.create_order = one_reject
    request = pair_intent()
    rows, _ = asyncio.run(orders.submit_pair(request))
    assert [row["status"] for row in rows] == ["resting", "rejected"]
    assert len([row for row in store.list() if row["pair_id"] == str(request.pair_id)]) == 2
    canceled = asyncio.run(orders.cancel_pair(str(request.pair_id)))
    assert [row["status"] for row in canceled] == ["cancel_pending", "rejected"]
    store.close()
    reopened_service, _, reopened = service(tmp_path, adapter)
    asyncio.run(reopened_service.startup_recovery())
    recovered = reopened.pair(str(request.pair_id))
    assert [row["status"] for row in recovered] == ["canceled", "rejected"]
    assert adapter.creates == 1
    reopened.close()


def test_pair_malformed_ack_keeps_both_leg_records(tmp_path):
    orders, adapter, store = service(tmp_path)
    original = adapter.create_order

    async def malformed(record):
        return None if record["outcome"] == "no" else await original(record)

    adapter.create_order = malformed
    request = pair_intent()
    rows, _ = asyncio.run(orders.submit_pair(request))
    assert [row["status"] for row in rows] == ["resting", "unknown"]
    assert store.pair(str(request.pair_id))[1]["client_order_id"]
    store.close()


def test_store_migrates_old_market_slot_without_losing_orders(tmp_path):
    config = Config(environment="demo", trading_enabled=False, db_path=tmp_path / "legacy.sqlite3")
    store = Store(config)
    legacy = intent()
    store.insert(legacy, datetime.fromtimestamp(1800000000, timezone.utc))
    store.close()
    with sqlite3.connect(config.db_path) as db:
        db.execute("DROP INDEX one_active_leg")
        db.execute("DROP INDEX one_pair_leg")
        for name in ("pair_intent", "leg_index", "pair_id"):
            db.execute(f"ALTER TABLE orders DROP COLUMN {name}")
        db.execute("""CREATE UNIQUE INDEX one_active_market ON orders(environment, account_key, ticker)
            WHERE status IN ('submitting','resting','cancel_pending','unknown')""")
    reopened = Store(config)
    assert reopened.get(str(legacy.request_id))["status"] == "submitting"
    assert not any(row[1] == "one_active_market" for row in reopened.db.execute("PRAGMA index_list(orders)"))
    request = pair_intent(primary_price="0.46", second_price="0.51")
    with pytest.raises(sqlite3.IntegrityError):
        reopened.insert_pair(request, request.legs(), datetime.fromtimestamp(1800000000, timezone.utc))
    # The legacy YES leg still blocks YES, while NO can occupy its own slot.
    no = intent(outcome="no")
    row, created = reopened.insert(no, datetime.fromtimestamp(1800000000, timezone.utc))
    assert created and row["outcome"] == "no"
    reopened.close()


def test_pair_http_read_only_and_two_leg_response(tmp_path):
    config = Config(environment="demo", trading_enabled=True, api_key_id="fake-key",
                    private_key_path="fake-path", db_path=tmp_path / "pair-http.sqlite3")
    adapter = FakeAdapter()
    app = create_app(config, adapter)
    with TestClient(app, base_url="http://localhost") as client:
        feed = app.state.feed
        feed.connected = True
        feed.last_pong = time.monotonic()
        feed.private_sid = 1
        feed.private_reconciled = True
        book = app.state.books.get("TEST-MARKET")
        book.subscribe(1, 2)
        book.snapshot(1, 2, 1, {"yes_dollars_fp": [["0.46", "20"]], "no_dollars_fp": [["0.51", "15"]]})
        client.get("/api/markets/TEST-MARKET/summary")
        token = client.get("/api/status").json()["request_token"]
        body = pair_intent().model_dump(mode="json")
        headers = {"Origin": "http://localhost", "X-Request-Token": token}
        result = client.post("/api/pairs", json=body, headers=headers)
        assert result.status_code == 201 and len(result.json()) == 2
        assert "exchange;dur=" in result.headers["Server-Timing"]
        assert client.post("/api/pairs", json=body, headers=headers).status_code == 200
        assert client.get(f"/api/pairs/{body['pair_id']}").status_code == 200
        assert adapter.creates == 2
