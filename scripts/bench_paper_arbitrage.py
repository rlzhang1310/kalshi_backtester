"""Synthetic local book burst benchmark; never connects to Kalshi."""

import asyncio
import json
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.books import BookRegistry
from app.paper import FeeModel, PaperDesk, PaperPair


async def main():
    registry = BookRegistry()
    levels = [[str(Decimal("0.3900") + Decimal(i) / 1000), "2.00"] for i in range(100)]
    for ticker in ("BENCH-A", "BENCH-B"):
        book = registry.get(ticker)
        book.subscribe(1, 7)
        book.snapshot(1, 7, 1, {"yes_dollars_fp": levels, "no_dollars_fp": levels})
    feed = SimpleNamespace(healthy=True, generation=1, wake=asyncio.Event())
    markets = SimpleNamespace(config=SimpleNamespace(metadata_ttl_seconds=3600))
    store = SimpleNamespace(unresolved=lambda: [])
    with tempfile.TemporaryDirectory() as directory:
        desk = PaperDesk(registry, markets, feed, store, "benchmark", Path(directory))
        pair = PaperPair("BENCH-A", "yes", "BENCH-B", "yes", "same_outcome",
                         Decimal("1"), Decimal(0), Decimal(0), True, True, 1)
        now = time.monotonic()
        metadata = {ticker: ({"status": "active", "price_ranges": [
            {"start": "0", "end": "1", "step": "0.0001"}]}, now)
            for ticker in ("BENCH-A", "BENCH-B")}
        fees = {ticker: FeeModel("quadratic", Decimal(1), "synthetic-v1")
                for ticker in metadata}
        desk.configure(pair, metadata, fees)
        desk.start()
        book = registry.get("BENCH-A")
        began = time.perf_counter()
        try:
            for index in range(5000):
                received = time.perf_counter_ns()
                book.delta(1, 7, index + 2, {"side": "no", "price_dollars": "0.45",
                                               "delta_fp": "0.01" if index % 2 == 0 else "-0.01"})
                applied = time.perf_counter_ns()
                book.last_received_ns = received
                desk.evaluate("BENCH-A", received, applied)
                if index % 100 == 99:
                    await asyncio.sleep(0)
            baseline = desk.view()["latency_ms"]
            await desk.evidence.join()

            thin = [[str(Decimal("0.1000") + Decimal(i) / 1000), "2.00"] for i in range(100)]
            a = registry.get("BENCH-A")
            b = registry.get("BENCH-B")
            for ticker, target in (("BENCH-A", a), ("BENCH-B", b)):
                target.subscribe(2, 8)
                target.snapshot(2, 8, 1, {"yes_dollars_fp": thin + ([["0.60", "2"]] if ticker == "BENCH-B" else []),
                                           "no_dollars_fp": thin + ([["0.60", "2"]] if ticker == "BENCH-A" else [])})
            feed.generation = 2
            desk.configure(PaperPair(**(vars(pair) | {"version": 2})), metadata, fees)
            desk.start()
            for index in range(400):
                received = time.perf_counter_ns()
                a.delta(2, 8, index + 2, {"side": "no", "price_dollars": "0.60",
                                          "delta_fp": "-2" if index % 2 == 0 else "2"})
                applied = time.perf_counter_ns()
                a.last_received_ns = received
                desk.evaluate("BENCH-A", received, applied)
                if index % 20 == 19:
                    await asyncio.sleep(0)
            await desk.evidence.join()
            print(json.dumps({"updates": 5000, "qualifying_updates": 400,
                              "elapsed_seconds": round(time.perf_counter() - began, 3),
                              "baseline_latency_ms": baseline,
                              "qualifying_latency_ms": desk.view()["latency_ms"],
                              "intent_latency_samples": len(desk.latency["decision_to_intent"]),
                              "evidence_overflow": desk.evidence_overflow}, indent=2))
        finally:
            await desk.close()


if __name__ == "__main__":
    asyncio.run(main())
