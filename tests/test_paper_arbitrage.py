"""Deterministic paper replay; no exchange credentials or order mutations."""

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.books import BookRegistry
from app.config import Config
from app.main import create_app
from app.paper import FeeModel, PaperDesk, PaperPair


class FakeStore:
    def __init__(self):
        self.orders = []

    def unresolved(self):
        return self.orders


def book(registry, ticker, yes, no, *, sequence=1):
    item = registry.get(ticker)
    item.subscribe(1, 7)
    item.snapshot(1, 7, sequence, {"yes_dollars_fp": yes, "no_dollars_fp": no})
    return item


def pair(relationship="same_outcome", **changes):
    values = dict(primary_ticker="A", primary_outcome="yes", other_ticker="B", other_outcome="yes",
                  relationship=relationship, quantity=Decimal("1"), min_profit=Decimal(0),
                  safety_margin=Decimal(0), direct_account=True, settlement_asserted=True, version=1)
    values.update(changes)
    return PaperPair(**values)


def setup(tmp_path, *, a_no="0.60", b_yes="0.60", a_yes="0.30", b_no="0.30",
          a_no_levels=None, b_yes_levels=None, fee_multiplier="1", relationship="same_outcome"):
    registry = BookRegistry()
    book(registry, "A", [[a_yes, "2.00"]], a_no_levels or [[a_no, "2.00"]])
    book(registry, "B", b_yes_levels or [[b_yes, "2.00"]], [[b_no, "2.00"]])
    feed = SimpleNamespace(healthy=True, generation=1, wake=asyncio.Event())
    markets = SimpleNamespace(config=SimpleNamespace(metadata_ttl_seconds=3600))
    store = FakeStore()
    desk = PaperDesk(registry, markets, feed, store, "demo", tmp_path / "paper")
    now = time.monotonic()
    metadata = {ticker: ({"status": "active", "price_ranges": [{"start": "0", "end": "1", "step": "0.0001"}]}, now)
                for ticker in ("A", "B")}
    fees = {ticker: FeeModel("quadratic", Decimal(fee_multiplier), "synthetic-v1") for ticker in ("A", "B")}
    desk.configure(pair(relationship), metadata, fees)
    return desk, registry, feed, store


def drive(desk, ticker="A"):
    now = time.perf_counter_ns()
    desk.evaluate(ticker, now, now + 1000)


def test_positive_edge_both_orientations_and_episode_dedup(tmp_path):
    async def run():
        desk, _, _, _ = setup(tmp_path)
        try:
            desk.start()
            assert desk.current[0]["legs"][0]["outcome"] == "yes"
            assert desk.current[0]["legs"][1]["outcome"] == "no"
            assert Decimal(desk.current[0]["net_edge"]) > 0
            assert len(desk.sink.intents) == 1
            for _ in range(10):
                drive(desk, "B")
            assert len(desk.sink.intents) == 1
            assert len(desk.episodes) == 1
            await asyncio.sleep(.12)
            assert {result["delay_ms"] for result in desk.results} == {0, 25, 50, 100}
            assert all(result["paired_fill"] for result in desk.results)
            pnl = desk.view()["simulated_pnl"]
            assert {row["delay_ms"] for row in pnl} == {0, 25, 50, 100}
            assert all(row["attempts"] == 1 and row["paired"] == 1 for row in pnl)
            assert all(Decimal(row["paired_net"]) > 0 for row in pnl)
            assert desk.view()["evidence_overflow"] == 0
            desk.stop()
            assert desk.view()["simulated_pnl"] == pnl
        finally:
            await desk.close()
    asyncio.run(run())


def test_opposite_relationship_and_reverse_orientation(tmp_path):
    async def run():
        desk, _, _, _ = setup(tmp_path, relationship="opposite_outcomes", a_no="0.60", b_no="0.60",
                                a_yes="0.30", b_yes="0.30")
        try:
            desk.start()
            assert [(leg["ticker"], leg["outcome"]) for leg in desk.current[0]["legs"]] == [("A", "yes"), ("B", "yes")]
            assert [(leg["ticker"], leg["outcome"]) for leg in desk.current[1]["legs"]] == [("A", "no"), ("B", "no")]
            assert Decimal(desk.current[0]["net_edge"]) > 0
        finally:
            await desk.close()
    asyncio.run(run())


def test_selected_no_outcomes_map_to_complementary_buys():
    same = pair("same_outcome", primary_outcome="no", other_outcome="no")
    assert same.orientations() == ((('A', 'no'), ('B', 'yes')),
                                   (('A', 'yes'), ('B', 'no')))
    opposite_pair = pair("opposite_outcomes", primary_outcome="no", other_outcome="yes")
    assert opposite_pair.orientations() == ((('A', 'no'), ('B', 'yes')),
                                            (('A', 'yes'), ('B', 'no')))


def test_observed_edge_without_profitable_caps_does_not_submit(tmp_path):
    async def run():
        desk, _, _, _ = setup(tmp_path, a_yes="0.29", a_no_levels=[["0.70", "0.50"], ["0.51", "0.50"]],
                                b_yes_levels=[["0.60", "1.00"]])
        current = desk.config
        desk.configure(PaperPair(**(vars(current) | {"min_profit": Decimal("0.15"), "version": 2})),
                       desk.metadata, desk.fees)
        try:
            desk.start()
            assert Decimal(desk.current[0]["net_edge"]) > Decimal("0.15")
            assert "Caps and conservative fees erase threshold" in desk.current[0]["blocked"]
            assert not desk.sink.intents
        finally:
            await desk.close()
    asyncio.run(run())


def test_fees_remove_gross_edge_and_exact_zero(tmp_path):
    async def run():
        desk, _, _, _ = setup(tmp_path, a_no="0.51", b_yes="0.51")
        try:
            desk.start()
            assert Decimal(desk.current[0]["gross_gap"]) > 0
            assert Decimal(desk.current[0]["net_edge"]) < 0
            assert not desk.sink.intents
        finally:
            await desk.close()
        zero, _, _, _ = setup(tmp_path, a_no="0.5002", b_yes="0.5002", fee_multiplier="0")
        try:
            zero.start()
            assert Decimal(zero.current[0]["net_edge"]) == 0
            assert not zero.sink.intents
        finally:
            await zero.close()
    asyncio.run(run())


def test_fractional_depth_and_insufficient_depth(tmp_path):
    async def run():
        desk, _, _, _ = setup(tmp_path, a_no_levels=[["0.61", "0.40"], ["0.60", "0.60"]],
                                b_yes_levels=[["0.60", "1.00"]])
        try:
            desk.start()
            leg = desk.current[0]["legs"][0]
            assert leg["available"] == "1"
            assert len(leg["depth"]) == 2
            assert leg["ask_cost"] == "0.396"
        finally:
            await desk.close()
        short, _, _, _ = setup(tmp_path, a_no_levels=[["0.60", "0.60"]])
        try:
            short.start()
            assert "Insufficient visible ask depth after app-owned orders" in short.current[0]["blocked"]
            assert not short.sink.intents
        finally:
            await short.close()
    asyncio.run(run())


def test_own_liquidity_gap_and_quiet_book(tmp_path):
    async def run():
        desk, registry, feed, store = setup(tmp_path)
        store.orders = [{"status": "resting", "ticker": "A", "outcome": "no", "limit_price": "0.6",
                         "remaining_quantity": "2"}]
        desk.refresh_owned()
        try:
            desk.start()
            assert desk.current[0]["legs"][0]["available"] == "0"
            store.orders = []
            desk.refresh_owned()
            registry.get("A").last_book_change_at = "2000-01-01T00:00:00+00:00"
            drive(desk)
            assert Decimal(desk.current[0]["net_edge"]) > 0  # Quiet synchronized books remain usable.
            feed.healthy = False
            drive(desk)
            assert any("not synchronized" in reason for reason in desk.current[0]["blocked"])
            assert desk.episodes[0]["ended_at"] is not None
        finally:
            await desk.close()
    asyncio.run(run())


def test_one_leg_arrival_and_configuration_reset(tmp_path):
    async def run():
        desk, registry, _, _ = setup(tmp_path)
        try:
            desk.start()
            await asyncio.sleep(.005)  # Zero-delay scenario has arrived.
            b = registry.get("B")
            b.delta(1, 7, 2, {"side": "yes", "price_dollars": "0.60", "delta_fp": "-2.00"})
            drive(desk, "B")
            assert desk.episodes[0]["ended_at"] is not None
            await asyncio.sleep(.12)
            delayed = [result for result in desk.results if result["delay_ms"] in {25, 50, 100}]
            assert delayed and all(result["unmatched_exposure"] for result in delayed)
            assert all(result["modeled_net"] is None for result in delayed)
            assert all(Decimal(row["unmatched_outlay"]) > 0 for row in desk.view()["simulated_pnl"]
                       if row["delay_ms"] in {25, 50, 100})
            current = desk.config
            desk.configure(PaperPair(**(vars(current) | {"version": 2})), desk.metadata, desk.fees)
            assert not desk.sink.intents and not desk.results and not desk.episodes
            assert all(row["attempts"] == 0 and row["paired_net"] == "0"
                       for row in desk.view()["simulated_pnl"])
        finally:
            await desk.close()
    asyncio.run(run())


def test_subcent_grid_gap_and_custom_delays(tmp_path):
    async def run():
        desk, registry, feed, _ = setup(tmp_path, a_no="0.6001", b_yes="0.6002")
        try:
            current = desk.config
            desk.configure(PaperPair(**(vars(current) | {"version": 2, "delays_ms": (0, 10)})),
                           desk.metadata, desk.fees)
            desk.start()
            assert desk.current[0]["legs"][0]["worst_ask"] == "0.3999"
            assert {result["delay_ms"] for result in desk.results} == set()
            await asyncio.sleep(.025)
            assert {result["delay_ms"] for result in desk.results} == {0, 10}
            desk.metadata["A"][0]["price_ranges"] = [{"start": "0", "end": "1", "step": "0.01"}]
            drive(desk)
            assert any("price grid" in reason for reason in desk.current[0]["blocked"])
            desk.metadata["A"][0]["price_ranges"] = [{"start": "0", "end": "1", "step": "0.0001"}]
            registry.get("A").invalidate()
            drive(desk)
            assert any("not synchronized" in reason for reason in desk.current[0]["blocked"])
            registry.get("A").subscribe(2, 8)
            registry.get("A").snapshot(2, 8, 1, {"yes_dollars_fp": [["0.30", "2"]],
                                                   "no_dollars_fp": [["0.6001", "2"]]})
            feed.generation = 2
            drive(desk)
            assert any("not synchronized" in reason for reason in desk.current[0]["blocked"])
            registry.get("B").subscribe(2, 8)
            registry.get("B").snapshot(2, 8, 1, {"yes_dollars_fp": [["0.6002", "2"]],
                                                   "no_dollars_fp": [["0.30", "2"]]})
            drive(desk)
            assert not desk.current[0]["blocked"]
        finally:
            await desk.close()
    asyncio.run(run())


def test_paper_api_never_mutates_exchange_with_live_manual_trading_enabled(tmp_path):
    class Adapter:
        key = None  # Keep the background WebSocket idle in this synthetic replay.
        private_ready = True
        trade_scope_status = "allowed"

        def __init__(self):
            self.mutations = 0

        async def check_trade_scope(self):
            return None

        async def get_market(self, ticker):
            return {"ticker": ticker, "event_ticker": f"EVENT-{ticker}", "status": "active",
                    "price_ranges": [{"start": "0", "end": "1", "step": "0.0001"}]}

        async def get_event(self, ticker):
            return {"series_ticker": "SERIES", "last_updated_ts": "2026-10-07T00:00:00Z"}

        async def get_series(self, ticker):
            return {"fee_type": "quadratic", "fee_multiplier": 1,
                    "last_updated_ts": "2026-10-07T00:00:00Z"}

        async def create_order(self, *_args, **_kwargs):
            self.mutations += 1
            raise AssertionError("Paper desk attempted an exchange create")

        async def cancel_order(self, *_args, **_kwargs):
            self.mutations += 1
            raise AssertionError("Paper desk attempted an exchange cancel")

        async def close(self):
            return None

    adapter = Adapter()
    config = Config(environment="demo", trading_enabled=True, api_key_id="fake",
                    private_key_path="fake", db_path=tmp_path / "orders.sqlite3")
    app = create_app(config, adapter)
    with TestClient(app, base_url="http://localhost") as client:
        token = client.get("/api/status").json()["request_token"]
        headers = {"Origin": "http://localhost", "X-Request-Token": token}
        response = client.post("/api/paper/configure", json={
            "primary_ticker": "TEST-A", "primary_outcome": "yes", "other_ticker": "TEST-B",
            "other_outcome": "yes", "relationship": "same_outcome", "quantity": "1",
            "direct_account": False, "settlement_asserted": True}, headers=headers)
        assert response.status_code == 200, response.text
        feed = app.state.feed
        feed.connected, feed.last_pong, feed.generation = True, time.monotonic(), 1
        for ticker, yes, no in (("TEST-A", "0.30", "0.60"), ("TEST-B", "0.60", "0.30")):
            book(app.state.books, ticker, [[yes, "2.00"]], [[no, "2.00"]])
        response = client.post("/api/paper/start", json={}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["would_submit"]
        assert client.post("/api/paper/stop", json={}, headers=headers).status_code == 200
        assert adapter.mutations == 0
