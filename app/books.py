"""Full exchange books and small, derived display views."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.models import canonical, decimal


class BookInvalid(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def level_price(value) -> Decimal:
    price = decimal(value)
    if not 0 <= price <= 1 or price.as_tuple().exponent < -4:
        raise BookInvalid("Invalid book price")
    return price


def level_quantity(value, *, signed=False) -> Decimal:
    quantity = decimal(value)
    if not signed and quantity < 0:
        raise BookInvalid("Negative snapshot quantity")
    return quantity


@dataclass
class LiveBook:
    ticker: str
    state: str = "connecting"
    generation: int = 0
    sid: int | None = None
    seq: int | None = None
    yes: dict[Decimal, Decimal] = field(default_factory=dict)
    no: dict[Decimal, Decimal] = field(default_factory=dict)
    snapshot_at: str | None = None
    last_book_change_at: str | None = None

    def subscribe(self, generation: int, sid: int):
        self.generation, self.sid, self.seq = generation, sid, None
        self.state = "syncing"

    def invalidate(self):
        self.state = "recovering"
        self.sid = None
        self.seq = None

    def snapshot(self, generation: int, sid: int, seq: int, message: dict):
        if generation != self.generation or sid != self.sid or not isinstance(seq, int):
            raise BookInvalid("Snapshot identity mismatch")
        yes, no = {}, {}
        for side, dest in (("yes_dollars_fp", yes), ("no_dollars_fp", no)):
            # Kalshi omits a side entirely when its book has no resting levels.
            levels = message.get(side, [])
            if not isinstance(levels, list):
                raise BookInvalid("Malformed snapshot")
            for level in levels:
                if not isinstance(level, list) or len(level) != 2:
                    raise BookInvalid("Malformed snapshot level")
                price, quantity = level_price(level[0]), level_quantity(level[1])
                if price in dest:
                    raise BookInvalid("Duplicate snapshot price")
                if quantity > 0:
                    dest[price] = quantity
        self._check(yes, no)
        self.yes, self.no, self.seq = yes, no, seq
        self.snapshot_at = self.last_book_change_at = now_iso()
        self.state = "live"

    def delta(self, generation: int, sid: int, seq: int, message: dict, *, shared_sequence=False):
        if self.state != "live" or generation != self.generation or sid != self.sid:
            raise BookInvalid("Delta without live snapshot")
        if not isinstance(seq, int) or (not shared_sequence and seq != self.seq + 1):
            raise BookInvalid("Book sequence gap")
        side = message["side"]
        if side not in {"yes", "no"}:
            raise BookInvalid("Invalid book side")
        price = level_price(message["price_dollars"])
        change = level_quantity(message["delta_fp"], signed=True)
        target = self.yes if side == "yes" else self.no
        updated = target.get(price, Decimal(0)) + change
        if updated < 0:
            raise BookInvalid("Negative resulting quantity")
        new_yes, new_no = self.yes.copy(), self.no.copy()
        new_target = new_yes if side == "yes" else new_no
        if updated == 0:
            new_target.pop(price, None)
        else:
            new_target[price] = updated
        self._check(new_yes, new_no)
        self.yes, self.no, self.seq = new_yes, new_no, seq
        if change != 0:
            self.last_book_change_at = now_iso()

    @staticmethod
    def _check(yes: dict, no: dict):
        if yes and no and max(yes) + max(no) >= 1:
            raise BookInvalid("Crossed book")

    def view(self, healthy: bool) -> dict:
        state = self.state if healthy or self.state != "live" else "recovering"
        yes_bids = sorted(self.yes.items(), reverse=True)
        no_bids = sorted(self.no.items(), reverse=True)

        def levels(source, complement=False):
            return [{"price": canonical(1 - price if complement else price), "quantity": canonical(qty)}
                    for price, qty in source[:5]]

        def outcome(bids, opposite):
            bid, ask = (bids[0] if bids else None), (opposite[0] if opposite else None)
            bid_price = bid[0] if bid else None
            ask_price = 1 - ask[0] if ask else None
            quote = {
                "bid_price": canonical(bid_price) if bid else None,
                "bid_quantity": canonical(bid[1]) if bid else None,
                "ask_price": canonical(ask_price) if ask else None,
                "ask_quantity": canonical(ask[1]) if ask else None,
                "spread": canonical(ask_price - bid_price) if bid and ask else None,
            }
            return quote, {"bids": levels(bids), "asks": levels(opposite, True)}

        yes_quote, yes_depth = outcome(yes_bids, no_bids)
        no_quote, no_depth = outcome(no_bids, yes_bids)
        return {
            "book_state": state, "snapshot_at": self.snapshot_at,
            "last_book_change_at": self.last_book_change_at, "stale": state != "live",
            "quotes": {"yes": yes_quote, "no": no_quote},
            "depth": {"yes": yes_depth, "no": no_depth},
        }


class BookRegistry:
    def __init__(self):
        self.books: dict[str, LiveBook] = {}
        self.revision = 0
        self.on_change = None

    def get(self, ticker: str) -> LiveBook:
        if ticker not in self.books:
            self.books[ticker] = LiveBook(ticker)
        return self.books[ticker]

    def touch(self):
        self.revision += 1
        if self.on_change:
            self.on_change()

    def disconnect(self):
        for book in self.books.values():
            book.invalidate()
        self.touch()
