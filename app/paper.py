"""Read-only, event-driven paper arbitrage observation.

The intent sink has no exchange adapter. All prices and quantities use Decimal.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from uuid import uuid4

from app.models import canonical, decimal
from app.markets import on_grid


ONE = Decimal("1")
MICRO = Decimal("0.000001")
FEE_RATE = Decimal("0.07")  # Kalshi event-contract schedule effective 2026-07-07.
DELAYS_MS = (0, 25, 50, 100)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def ceil_micro(value: Decimal) -> Decimal:
    return value.quantize(MICRO, rounding=ROUND_CEILING)


def opposite(outcome: str) -> str:
    return "no" if outcome == "yes" else "yes"


@dataclass(frozen=True)
class FeeModel:
    fee_type: str
    multiplier: Decimal
    version: str

    @property
    def supported(self) -> bool:
        return self.fee_type in {"quadratic", "quadratic_with_maker_fees"} and self.multiplier >= 0

    def bound(self, fills: tuple[tuple[Decimal, Decimal], ...], quantity: Decimal,
              *, direct_account: bool) -> Decimal:
        """Upper estimate: model fee, fragmentation micro-rounding, and one balance alignment."""
        model = sum((FEE_RATE * self.multiplier * size * price * (ONE - price)
                     for price, size in fills), Decimal(0))
        fragments = int((quantity / Decimal("0.01")).to_integral_value(rounding=ROUND_CEILING))
        precision = Decimal("0.0001") if direct_account else Decimal("0.01")
        return ceil_micro(model) + fragments * MICRO + precision


@dataclass(frozen=True)
class PaperPair:
    primary_ticker: str
    primary_outcome: str
    other_ticker: str
    other_outcome: str
    relationship: str
    quantity: Decimal
    min_profit: Decimal
    safety_margin: Decimal
    direct_account: bool
    settlement_asserted: bool
    version: int
    delays_ms: tuple[int, ...] = DELAYS_MS

    def orientations(self):
        other_buy = opposite(self.other_outcome) if self.relationship == "same_outcome" else self.other_outcome
        first = ((self.primary_ticker, self.primary_outcome), (self.other_ticker, other_buy))
        second = tuple((ticker, opposite(side)) for ticker, side in first)
        return tuple(dict.fromkeys((first, second)))


class PaperIntentSink:
    """Records proposed IOC buys; deliberately has no order transport or exchange handle."""

    def __init__(self):
        self.intents = deque(maxlen=80)

    def record(self, intent: dict):
        self.intents.appendleft(intent)


class PaperDesk:
    def __init__(self, books, markets, feed, store, environment: str, evidence_dir: Path):
        self.books, self.markets, self.feed, self.store = books, markets, feed, store
        self.environment = environment
        self.evidence_dir = evidence_dir
        self.config: PaperPair | None = None
        self.fees: dict[str, FeeModel] = {}
        self.metadata: dict[str, tuple[dict, float]] = {}
        self.observing = False
        self.current: list[dict] = []
        self.episodes = deque(maxlen=80)
        self.sink = PaperIntentSink()
        self.results = deque(maxlen=160)
        self.pnl: dict[int, dict] = {}
        self.open_episodes: dict[int, dict] = {}
        self.shadow: dict[int, dict[tuple, Decimal]] = {delay: {} for delay in DELAYS_MS}
        self.tasks: set[asyncio.Task] = set()
        self.owned: dict[tuple[str, str, Decimal], Decimal] = {}
        self.evidence: asyncio.Queue = asyncio.Queue(maxsize=10000)
        self.evidence_overflow = 0
        self.evidence_path: Path | None = None
        self.writer_task: asyncio.Task | None = None
        self.latency = {name: deque(maxlen=1000) for name in ("receive_to_book", "book_to_decision", "decision_to_intent")}
        self.revision = 0
        self.on_change = None

    @property
    def tickers(self):
        return {self.config.primary_ticker, self.config.other_ticker} if self.config and (self.observing or self.tasks) else set()

    def touch(self):
        self.revision += 1
        if self.on_change:
            self.on_change()

    def refresh_owned(self):
        owned = {}
        for order in self.store.unresolved():
            if order["status"] not in {"resting", "cancel_pending", "unknown"}:
                continue
            key = (order["ticker"], order["outcome"], decimal(order["limit_price"]))
            owned[key] = owned.get(key, Decimal(0)) + decimal(order["remaining_quantity"])
        self.owned = owned

    def configure(self, pair: PaperPair, metadata: dict, fees: dict[str, FeeModel]):
        self.reset()
        self.config, self.metadata, self.fees = pair, metadata, fees
        self.shadow = {delay: {} for delay in pair.delays_ms}
        self.pnl = {delay: {"attempts": 0, "paired": 0, "unmatched": 0, "missed": 0,
                            "unpriced_exposure": 0, "paired_net": Decimal(0),
                            "unmatched_outlay": Decimal(0)} for delay in pair.delays_ms}
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_path = self.evidence_dir / f"{self.environment}-{uuid4().hex}.jsonl"
        self.refresh_owned()
        self.record({"type": "configuration", "at": stamp(), "environment": self.environment,
                     "pair": self.pair_view(), "fees": {key: vars(value) | {"multiplier": str(value.multiplier)}
                                                      for key, value in fees.items()}})
        if not self.writer_task or self.writer_task.done():
            self.writer_task = asyncio.create_task(self._write_evidence())
        self.touch()

    def reset(self):
        for task in tuple(self.tasks):
            task.cancel()
        self.tasks.clear()
        self.observing = False
        self.current = []
        self.open_episodes.clear()
        self.shadow = {delay: {} for delay in DELAYS_MS}
        self.episodes.clear()
        self.sink.intents.clear()
        self.results.clear()
        self.pnl.clear()
        self.evidence_overflow = 0
        self.config = None
        self.evidence_path = None
        self.feed.wake.set()
        self.touch()

    def start(self):
        if not self.config:
            raise ValueError("Lock and configure a pair first")
        self.observing = True
        self.feed.wake.set()
        self.touch()
        self.evaluate("start", time.perf_counter_ns(), time.perf_counter_ns())

    def stop(self):
        self.observing = False
        now = stamp()
        for episode in self.open_episodes.values():
            episode["ended_at"] = now
            episode["duration_ms"] = (time.perf_counter_ns() - episode["started_ns"]) / 1e6
            episode.pop("started_ns", None)
            episode["end_reason"] = "Observation stopped"
        self.open_episodes.clear()
        self.current = []
        self.feed.wake.set()
        self.touch()

    async def close(self):
        self.reset()
        if self.writer_task:
            await self.evidence.join()
            self.writer_task.cancel()
            try:
                await self.writer_task
            except asyncio.CancelledError:
                pass

    def record(self, item: dict):
        try:
            self.evidence.put_nowait((self.evidence_path, item))
        except asyncio.QueueFull:
            self.evidence_overflow += 1
            self.touch()

    async def _write_evidence(self):
        while True:
            item = await self.evidence.get()
            batch = [item]
            while len(batch) < 100:
                try:
                    batch.append(self.evidence.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                grouped = {}
                for path, row in batch:
                    grouped.setdefault(path, []).append(json.dumps(row, separators=(",", ":"), default=str) + "\n")
                for path, lines in grouped.items():
                    await asyncio.to_thread(self._append_file, path, "".join(lines))
            except OSError:
                self.evidence_overflow += len(batch)
                self.touch()
            finally:
                for _ in batch:
                    self.evidence.task_done()

    @staticmethod
    def _append_file(path, lines):
        current_size = path.stat().st_size if path.exists() else 0
        if current_size + len(lines.encode("utf8")) > 25_000_000:
            raise OSError("Paper evidence session limit reached")
        with path.open("a", encoding="utf8") as output:
            output.write(lines)

    def pair_view(self):
        if not self.config:
            return None
        pair = self.config
        return {"primary_ticker": pair.primary_ticker, "primary_outcome": pair.primary_outcome,
                "other_ticker": pair.other_ticker, "other_outcome": pair.other_outcome,
                "relationship": pair.relationship, "quantity": canonical(pair.quantity),
                "min_profit": canonical(pair.min_profit), "safety_margin": canonical(pair.safety_margin),
                "direct_account": pair.direct_account, "settlement_asserted": pair.settlement_asserted,
                 "version": pair.version, "delays_ms": pair.delays_ms}

    def _walk(self, ticker: str, outcome: str, quantity: Decimal, *, cap=None, shadow=None):
        book = self.books.get(ticker)
        source = opposite(outcome)
        bids = book.yes if source == "yes" else book.no
        prices = book.yes_prices if source == "yes" else book.no_prices
        remaining, cost, taken = quantity, Decimal(0), []
        for bid_price in reversed(prices):
            ask = ONE - bid_price
            if cap is not None and ask > cap:
                break
            key = (ticker, source, bid_price)
            available = bids[bid_price] - self.owned.get(key, Decimal(0))
            if shadow is not None:
                available -= shadow.get(key, Decimal(0))
            if available <= 0:
                continue
            size = min(remaining, available)
            taken.append((ask, size, bid_price))
            cost += ask * size
            remaining -= size
            if remaining <= 0:
                break
        return {"filled": quantity - remaining, "cost": cost,
                "worst": taken[-1][0] if taken else None, "levels": tuple(taken)}

    def _book_reason(self, ticker: str):
        book = self.books.get(ticker)
        if not self.feed.healthy or book.state != "live" or book.generation != self.feed.generation or book.seq is None:
            return f"{ticker} book is not synchronized"
        market, updated = self.metadata.get(ticker, ({}, 0))
        if market.get("status") not in {"active", "open"} or not market.get("price_ranges"):
            return f"{ticker} is not eligible"
        if time.monotonic() - updated > self.markets.config.metadata_ttl_seconds:
            return f"{ticker} metadata expired"
        return None

    def _fee(self, ticker, walk, quantity):
        model = self.fees.get(ticker)
        if not model or not model.supported:
            return None
        return model.bound(tuple((price, size) for price, size, _ in walk["levels"]), quantity,
                           direct_account=self.config.direct_account)

    def evaluate(self, changed_ticker: str, received_ns: int, applied_ns: int):
        if not self.observing or not self.config or changed_ticker not in self.tickers | {"start", "refresh"}:
            return
        start_ns = time.perf_counter_ns()
        self.latency["receive_to_book"].append(max(0, (applied_ns - received_ns) / 1e6))
        pair = self.config
        observations = []
        for index, legs in enumerate(pair.orientations()):
            reasons = [reason for ticker, _ in legs if (reason := self._book_reason(ticker))]
            if not pair.settlement_asserted:
                reasons.append("Settlement equivalence has not been asserted")
            if self.evidence_overflow:
                reasons.append("Evidence queue overflow")
            if (applied_ns - received_ns) / 1e6 > 250:
                reasons.append("Local book processing lag exceeded 250 ms")
            walks = [self._walk(ticker, outcome, pair.quantity) for ticker, outcome in legs]
            if any(walk["filled"] < pair.quantity for walk in walks):
                reasons.append("Insufficient visible ask depth after app-owned orders")
            for (ticker, _), walk in zip(legs, walks):
                ranges = self.metadata.get(ticker, ({}, 0))[0].get("price_ranges", [])
                if walk["levels"] and not all(on_grid(price, ranges) for price, _, _ in walk["levels"]):
                    reasons.append(f"{ticker} ask is outside the current price grid")
            fees = [self._fee(ticker, walk, pair.quantity) for (ticker, _), walk in zip(legs, walks)]
            if any(fee is None for fee in fees):
                reasons.append("Fee model unavailable or unsupported")
            cost = sum((walk["cost"] for walk in walks), Decimal(0))
            fee_total = sum((fee for fee in fees if fee is not None), Decimal(0))
            edge = pair.quantity - cost - fee_total if all(fee is not None for fee in fees) and all(
                walk["filled"] == pair.quantity for walk in walks) else None
            threshold = pair.min_profit + pair.safety_margin
            record = {"orientation": index, "at": stamp(), "changed_ticker": changed_ticker,
                      "changed_received_monotonic_ns": received_ns,
                      "processed_monotonic_ns": time.perf_counter_ns(),
                      "legs": [{"ticker": ticker, "outcome": outcome, "ask_cost": canonical(walk["cost"]),
                                "available": canonical(walk["filled"]),
                                "worst_ask": canonical(walk["worst"]) if walk["worst"] is not None else None,
                                "fee_bound": canonical(fee) if fee is not None else None,
                                "depth": [{"ask": canonical(price), "quantity": canonical(size)}
                                          for price, size, _ in walk["levels"]],
                                "generation": self.books.get(ticker).generation,
                                "subscription_id": self.books.get(ticker).sid,
                                "revision": self.books.get(ticker).seq,
                                 "received_monotonic_ns": self.books.get(ticker).last_received_ns,
                                "fee_version": self.fees[ticker].version if ticker in self.fees else None}
                               for (ticker, outcome), walk, fee in zip(legs, walks, fees)],
                      "purchase_cost": canonical(cost), "fee_bound": canonical(fee_total) if None not in fees else None,
                      "net_edge": canonical(edge) if edge is not None else None,
                      "threshold": canonical(threshold), "blocked": reasons,
                      "gross_gap": canonical(pair.quantity - cost) if all(walk["filled"] == pair.quantity for walk in walks) else None,
                      "config_version": pair.version}
            observations.append(record)
            is_opportunity = edge is not None and edge > 0 and not reasons
            episode = self.open_episodes.get(index)
            if is_opportunity:
                if episode is None:
                    episode = {"id": uuid4().hex, "orientation": index, "started_at": record["at"],
                               "started_ns": time.perf_counter_ns(), "peak_edge": canonical(edge),
                               "attempted": False, "ended_at": None}
                    self.open_episodes[index] = episode
                    self.episodes.appendleft(episode)
                elif edge > decimal(episode["peak_edge"]):
                    episode["peak_edge"] = canonical(edge)
                if not episode["attempted"] and edge >= threshold:
                    caps = [walk["worst"] for walk in walks]
                    worst_fees = []
                    for (ticker, _), cap in zip(legs, caps):
                        low = min(price for price, _, _ in walks[len(worst_fees)]["levels"])
                        high_fee_price = Decimal("0.5") if low <= Decimal("0.5") <= cap else max(
                            (low, cap), key=lambda price: price * (ONE - price))
                        model = self.fees[ticker]
                        worst_fees.append(model.bound(((high_fee_price, pair.quantity),), pair.quantity,
                                                      direct_account=pair.direct_account))
                    cap_edge = pair.quantity - sum((cap * pair.quantity for cap in caps), Decimal(0)) - sum(worst_fees)
                    if cap_edge >= threshold:
                        decided_ns = time.perf_counter_ns()
                        intent = {"id": uuid4().hex, "episode_id": episode["id"], "at": stamp(),
                                  "policy": "IOC limit buys; cancel each unfilled remainder; independent legs",
                                  "legs": [{"ticker": ticker, "outcome": outcome, "quantity": canonical(pair.quantity),
                                            "cap": canonical(cap)} for (ticker, outcome), cap in zip(legs, caps)],
                                  "observed_net_edge": canonical(edge), "cap_net_edge_bound": canonical(cap_edge),
                                  "threshold": canonical(threshold), "fee_versions": [self.fees[t].version for t, _ in legs],
                                  "source": record, "config_version": pair.version}
                        self.sink.record(intent)
                        episode["attempted"] = True
                        self.record({"type": "would_submit", **intent})
                        for delay in pair.delays_ms:
                            task = asyncio.create_task(self._simulate(intent, delay))
                            self.tasks.add(task)
                            task.add_done_callback(self.tasks.discard)
                        self.latency["decision_to_intent"].append((time.perf_counter_ns() - decided_ns) / 1e6)
                    else:
                        record["blocked"].append("Caps and conservative fees erase threshold")
            elif episode is not None:
                episode["ended_at"] = record["at"]
                episode["duration_ms"] = (time.perf_counter_ns() - episode["started_ns"]) / 1e6
                episode.pop("started_ns", None)
                episode["end_reason"] = " · ".join(reasons) if reasons else "No positive net edge"
                self.open_episodes.pop(index, None)
            self.record({"type": "decision", "environment": self.environment, "pair": self.pair_view(), **record})
        self.current = observations
        self.latency["book_to_decision"].append((time.perf_counter_ns() - applied_ns) / 1e6)
        self.touch()

    async def _simulate(self, intent: dict, delay_ms: int):
        await asyncio.sleep(delay_ms / 1000)
        pair = self.config
        if not pair or pair.version != intent["config_version"]:
            return
        shadow = self.shadow[delay_ms]
        legs = []
        for index, proposal in enumerate(intent["legs"]):
            ticker, outcome = proposal["ticker"], proposal["outcome"]
            blocked = self._book_reason(ticker)
            source = intent["source"]["legs"][index]
            book = self.books.get(ticker)
            if book.generation != source["generation"] or book.sid != source["subscription_id"]:
                blocked = "Feed gap or resubscription during delay"
            walk = self._walk(ticker, outcome, pair.quantity, cap=decimal(proposal["cap"]), shadow=shadow) if not blocked else None
            if walk:
                for _, size, bid_price in walk["levels"]:
                    key = (ticker, opposite(outcome), bid_price)
                    shadow[key] = shadow.get(key, Decimal(0)) + size
            filled = walk["filled"] if walk else Decimal(0)
            fee = self._fee(ticker, walk, filled) if walk and filled else Decimal(0)
            legs.append({"ticker": ticker, "outcome": outcome, "filled": canonical(filled),
                         "cost": canonical(walk["cost"]) if walk else "0", "fee_bound": canonical(fee) if fee is not None else None,
                         "blocked": blocked, "generation": self.books.get(ticker).generation,
                         "revision": self.books.get(ticker).seq})
        paired = all(decimal(leg["filled"]) == pair.quantity and leg["fee_bound"] is not None for leg in legs)
        exposure = not paired and any(decimal(leg["filled"]) > 0 for leg in legs)
        outlay = (sum((decimal(leg["cost"]) + decimal(leg["fee_bound"]) for leg in legs), Decimal(0))
                  if all(leg["fee_bound"] is not None for leg in legs) else None)
        modeled_net = pair.quantity - outlay if paired and outlay is not None else None
        result = {"type": "simulated_result", "intent_id": intent["id"], "at": stamp(),
                  "delay_ms": delay_ms, "legs": legs, "paired_fill": paired,
                   "unmatched_exposure": exposure, "modeled_outlay": canonical(outlay) if outlay is not None else None,
                   "modeled_net": canonical(modeled_net) if modeled_net is not None else None,
                  "note": "Depth-based estimate, not an exchange fill or realized profit"}
        self.results.appendleft(result)
        bucket = self.pnl[delay_ms]
        bucket["attempts"] += 1
        if paired:
            bucket["paired"] += 1
            bucket["paired_net"] += modeled_net
        elif exposure:
            bucket["unmatched"] += 1
            if outlay is None:
                bucket["unpriced_exposure"] += 1
            else:
                bucket["unmatched_outlay"] += outlay
        else:
            bucket["missed"] += 1
        self.record(result)
        self.touch()
        self.feed.wake.set()

    def view(self):
        def percentiles(values):
            if not values:
                return None
            ordered = sorted(values)
            return {name: round(ordered[int((len(ordered) - 1) * fraction)], 3)
                    for name, fraction in (("p50", .5), ("p95", .95), ("p99", .99))}
        episodes = [{key: value for key, value in episode.items() if key != "started_ns"}
                    for episode in list(self.episodes)[:20]]
        return {"configured": self.config is not None, "observing": self.observing,
                "environment": self.environment, "pair": self.pair_view(), "current": self.current,
                "episodes": episodes, "would_submit": list(self.sink.intents)[:20],
                "simulated_results": list(self.results)[:40], "evidence_overflow": self.evidence_overflow,
                 "simulated_pnl": [{"delay_ms": delay, **{key: canonical(value) if isinstance(value, Decimal) else value
                                                        for key, value in bucket.items()}}
                                   for delay, bucket in sorted(self.pnl.items())],
                "evidence_file": str(self.evidence_path) if self.evidence_path else None,
                "own_liquidity_note": "App-owned resting size is excluded; other own orders cannot be identified from public depth",
                "latency_ms": {name: percentiles(values) for name, values in self.latency.items()},
                "revision": self.revision}
