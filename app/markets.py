import asyncio
import time
from decimal import Decimal

from app.books import BookRegistry
from app.models import canonical, decimal, ticker


def on_grid(price: Decimal, ranges: list[dict]) -> bool:
    for band in ranges:
        try:
            start, end, step = (decimal(band[key]) for key in ("start", "end", "step"))
            if step > 0 and start <= price <= end and (price - start) % step == 0:
                return True
        except (KeyError, ValueError):
            continue
    return False


def _best(levels: list) -> tuple[Decimal, Decimal] | None:
    valid: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError("Malformed orderbook level")
        price, quantity = decimal(level[0]), decimal(level[1])
        if not 0 < price < 1 or quantity < 0:
            raise ValueError("Invalid orderbook level")
        if quantity > 0:
            valid.append((price, quantity))
    if not valid:
        return None
    best_price = max(price for price, _ in valid)
    return best_price, sum((qty for price, qty in valid if price == best_price), Decimal(0))


def normalize_book(book: dict) -> dict:
    raw = book["orderbook_fp"]
    yes = _best(raw.get("yes_dollars", []))
    no = _best(raw.get("no_dollars", []))

    def quote(bid, ask):
        bid_price = bid[0] if bid else None
        ask_price = 1 - ask[0] if ask else None
        if bid_price is not None and ask_price is not None and bid_price >= ask_price:
            raise ValueError("Crossed orderbook")
        return {
            "bid_price": canonical(bid_price) if bid else None,
            "bid_quantity": canonical(bid[1]) if bid else None,
            "ask_price": canonical(ask_price) if ask else None,
            "ask_quantity": canonical(ask[1]) if ask else None,
            "spread": canonical(ask_price - bid_price) if bid and ask else None,
        }

    return {"yes": quote(yes, no), "no": quote(no, yes)}


class MarketService:
    def __init__(self, adapter, config, books: BookRegistry, healthy):
        self.adapter = adapter
        self.config = config
        self.books = books
        self.healthy = healthy
        self.cache: dict[str, tuple[dict, float]] = {}
        self.inflight: dict[str, asyncio.Task] = {}
        self.retry_after: dict[str, float] = {}
        self.retry_delay: dict[str, float] = {}
        self.lock = asyncio.Lock()
        self.on_change = None
        self.revision = 0

    async def summary(self, market_ticker: str, fresh: bool = False) -> dict:
        market_ticker = ticker(market_ticker)
        market, timestamp = await self.metadata(market_ticker, fresh=fresh)
        return self._compose(market_ticker, market, timestamp)

    def summary_cached(self, market_ticker: str) -> dict:
        market_ticker = ticker(market_ticker)
        cached = self.cache.get(market_ticker)
        if ((not cached or time.monotonic() - cached[1] >= self.config.metadata_ttl_seconds)
                and market_ticker not in self.inflight
                and time.monotonic() >= self.retry_after.get(market_ticker, 0)):
            task = asyncio.create_task(self.metadata(market_ticker, fresh=True))
            task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
        if cached:
            return self._compose(market_ticker, *cached)
        return self._compose(market_ticker, {"ticker": market_ticker, "status": "unavailable"}, 0)

    def _compose(self, market_ticker: str, market: dict, timestamp: float) -> dict:
        try:
            valid_ranges = bool(market.get("price_ranges")) and all(
                decimal(r["step"]) > 0 and decimal(r["start"]) <= decimal(r["end"])
                for r in market["price_ranges"]
            )
        except (KeyError, TypeError, ValueError):
            valid_ranges = False
        view = self.books.get(market_ticker).view(self.healthy())
        meta_current = time.monotonic() - timestamp <= self.config.metadata_ttl_seconds
        status = market.get("status", "unknown")
        return {
            "ticker": market_ticker,
            "title": market.get("title") or market_ticker,
            "yes_subtitle": market.get("yes_sub_title"),
            "no_subtitle": market.get("no_sub_title"),
            "status": status if valid_ranges else "unavailable",
            "rules": {"primary": market.get("rules_primary"), "secondary": market.get("rules_secondary")},
            "price_ranges": market.get("price_ranges") or [],
            "tradable": (view["book_state"] == "live" and meta_current and valid_ranges
                         and status in {"active", "open"}),
            **view,
        }

    async def metadata(self, market_ticker: str, fresh: bool = False) -> tuple[dict, float]:
        market_ticker = ticker(market_ticker)
        cached = self.cache.get(market_ticker)
        if not fresh and cached and time.monotonic() - cached[1] < self.config.metadata_ttl_seconds:
            return cached
        async with self.lock:
            task = self.inflight.get(market_ticker)
            if task is None:
                task = asyncio.create_task(self.adapter.get_market(market_ticker))
                self.inflight[market_ticker] = task
        try:
            market = await task
            result = (market, time.monotonic())
            self.cache[market_ticker] = result
            self.retry_after.pop(market_ticker, None)
            self.retry_delay.pop(market_ticker, None)
            self.revision += 1
            if self.on_change:
                self.on_change()
            return result
        except Exception:
            delay = self.retry_delay.get(market_ticker, 2.0)
            self.retry_after[market_ticker] = time.monotonic() + delay
            self.retry_delay[market_ticker] = min(30.0, delay * 2)
            if cached and not fresh:
                return cached
            raise
        finally:
            async with self.lock:
                if self.inflight.get(market_ticker) is task:
                    self.inflight.pop(market_ticker, None)
