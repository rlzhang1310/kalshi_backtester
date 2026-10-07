import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.kalshi import ExchangeError
from app.markets import on_grid
from app.models import TERMINAL, canonical, decimal


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def same_intent(record: dict, request) -> bool:
    return (record["ticker"], record["outcome"], record["mode"], record["limit_price"]) == (
        request.ticker, request.outcome, request.mode, request.price
    )


class OrderService:
    def __init__(self, config, adapter, markets, store, feed=None):
        self.config, self.adapter, self.markets, self.store = config, adapter, markets, store
        self.feed = feed
        self.recovering = True
        self.reconcile_lock = asyncio.Lock()
        self.background_tasks: set[asyncio.Task] = set()

    def reconcile_soon(self, request_id: str):
        async def follow_up():
            # Give the private stream a brief chance to deliver the same update.
            await asyncio.sleep(0.25)
            record = self.store.get(request_id)
            if record and (record["status"] in {"submitting", "unknown", "cancel_pending"} or
                           (decimal(record["filled_quantity"]) > 0 and
                            (record["average_fill_price"] is None or record["actual_fees"] is None))):
                try:
                    await self.reconcile(record)
                except Exception:
                    pass  # The periodic REST backstop will retry.

        task = asyncio.create_task(follow_up())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def stop(self):
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

    async def startup_recovery(self):
        failed = False
        for record in self.store.unresolved():
            try:
                await self.reconcile(record)
            except Exception:
                failed = True
        self.recovering = failed or any(
            row["status"] in {"submitting", "unknown"} for row in self.store.unresolved()
        )

    async def submit(self, request, timings: dict | None = None):
        started = time.perf_counter()
        request_id = str(request.request_id)
        existing = self.store.get(request_id)
        if existing:
            if existing["pair_id"] is not None or not same_intent(existing, request):
                raise AppError(409, "request_conflict", "This request ID already has different order details")
            return existing, False
        self._check_ready()
        try:
            summary = await self.markets.summary(request.ticker)
        except Exception as exc:
            raise AppError(503, "market_unavailable", "Could not refresh this market") from exc
        self._validate_leg(request, summary)
        if timings is not None:
            timings["validation"] = (time.perf_counter() - started) * 1000
        expiry = datetime.now(timezone.utc) + timedelta(seconds=self.config.expiry_seconds)
        try:
            record, created = self.store.insert(request, expiry)
        except sqlite3.IntegrityError as exc:
            raise AppError(409, "active_market_order", "This outcome already has an active app order") from exc
        if not created:
            if record["pair_id"] is not None or not same_intent(record, request):
                raise AppError(409, "request_conflict", "This request ID already has different order details")
            return record, False
        if self.feed is not None:
            self.feed.wake.set()
        record = await self._dispatch(record, timings)
        return record, True

    def _check_ready(self):
        if not self.config.trading_enabled:
            raise AppError(403, "trading_disabled", "Trading is disabled in backend configuration")
        trade_scope_status = getattr(self.adapter, "trade_scope_status", "unknown")
        if trade_scope_status == "read_only":
            raise AppError(403, "trade_scope_missing", "API key has read-only access; use a key with write or write::trade scope")
        if trade_scope_status == "wrong_subaccount":
            raise AppError(403, "trade_subaccount_mismatch", "API key is restricted to a different subaccount")
        if self.recovering:
            raise AppError(503, "recovering", "Order recovery is still running")
        if not self.adapter.private_ready:
            raise AppError(503, "authentication_unavailable", "Private exchange access is unavailable")
        if self.feed is not None and not self.feed.order_stream_ready:
            raise AppError(503, "order_stream_unavailable", "Private order stream is not ready")

    def _validate_leg(self, request, summary):
        if not summary["tradable"]:
            raise AppError(409, "market_unavailable", "Market is closed, paused, or lacks valid quote metadata")
        if summary["book_state"] != "live" or (self.feed is not None and not self.feed.healthy):
            raise AppError(503, "book_not_live", "Book is not synchronized")
        price = decimal(request.price)
        exchange_price = price if request.outcome == "yes" else 1 - price
        if not on_grid(price, summary["price_ranges"]) or not on_grid(exchange_price, summary["price_ranges"]):
            raise AppError(422, "off_grid", "Price is outside this market's valid price grid")
        ask = summary["quotes"][request.outcome]["ask_price"]
        if ask is not None and price >= decimal(ask):
            raise AppError(409, "would_take_liquidity", "This price would take liquidity. Choose a lower price.")
        if self.feed is not None and (not self.feed.order_stream_ready or
                                      self.markets.books.get(request.ticker).state != "live"):
            raise AppError(503, "book_not_live", "Book or private stream changed during validation")

    async def submit_pair(self, request, timings: dict | None = None):
        started = time.perf_counter()
        try:
            legs = request.legs()
        except ValueError as exc:
            raise AppError(422, "invalid_relationship", str(exc)) from exc
        if decimal(legs[0].price) + decimal(legs[1].price) > 1:
            raise AppError(422, "invalid_spread", "Paired buy limits imply a negative spread")
        intent = json.dumps(request.model_dump(mode="json"), sort_keys=True)
        pair_id = str(request.pair_id)
        existing = self.store.pair(pair_id)
        if existing:
            if len(existing) != 2 or any(row["pair_intent"] != intent for row in existing):
                raise AppError(409, "request_conflict", "This pair ID already has different order details")
            return existing, False
        self._check_ready()
        tickers = list(dict.fromkeys(leg.ticker for leg in legs))
        try:
            summaries = await asyncio.gather(*(self.markets.summary(value) for value in tickers))
        except Exception as exc:
            raise AppError(503, "market_unavailable", "Could not refresh a pair market") from exc
        by_ticker = dict(zip(tickers, summaries))
        for leg in legs:
            self._validate_leg(leg, by_ticker[leg.ticker])
        if timings is not None:
            timings["validation"] = (time.perf_counter() - started) * 1000
        expiry = datetime.now(timezone.utc) + timedelta(seconds=self.config.expiry_seconds)
        try:
            records, created = self.store.insert_pair(request, legs, expiry)
        except sqlite3.IntegrityError as exc:
            raise AppError(409, "active_market_order", "An outcome in this pair already has an active app order") from exc
        if not created:
            if len(records) != 2 or any(row["pair_intent"] != intent for row in records):
                raise AppError(409, "request_conflict", "This pair ID already has different order details")
            return records, False
        if self.feed is not None:
            self.feed.wake.set()
        # Both exchange requests start in the same event-loop turn; neither waits for the other's reply.
        exchange_started = time.perf_counter()
        results = await asyncio.gather(*(self._dispatch(row) for row in records), return_exceptions=True)
        for row, result in zip(records, results):
            if isinstance(result, Exception):
                latest = self.store.get(row["request_id"])
                if latest and latest["status"] == "submitting":
                    self.store.update(row["request_id"], status="unknown", error_code="submission_uncertain",
                                      message="Checking exchange for this request")
                    self.reconcile_soon(row["request_id"])
        if timings is not None:
            timings["exchange"] = (time.perf_counter() - exchange_started) * 1000
        return self.store.pair(pair_id), True

    async def _dispatch(self, record: dict, timings: dict | None = None):
        request_id = record["request_id"]
        exchange_started = time.perf_counter()
        try:
            acknowledgement = await self.adapter.create_order(record)
        except ExchangeError as exc:
            if timings is not None:
                timings["exchange"] = (time.perf_counter() - exchange_started) * 1000
            latest = self.store.get(request_id)
            if latest["status"] != "submitting":
                return latest
            if exc.status in (400, 401, 403, 422):
                message = ("Exchange denied permission to place this order (HTTP 403)" if exc.status == 403 else
                           "Exchange could not authenticate the order request (HTTP 401)" if exc.status == 401 else
                           "Exchange rejected the order")
                return self.store.update(request_id, status="rejected", remaining_quantity="0", error_code=exc.code,
                                         message=message)
            uncertain = self.store.update(request_id, status="unknown", error_code="submission_uncertain",
                                          message="Checking exchange for this request")
            self.reconcile_soon(request_id)
            return uncertain
        except Exception:
            if timings is not None:
                timings["exchange"] = (time.perf_counter() - exchange_started) * 1000
            latest = self.store.get(request_id)
            if latest["status"] != "submitting":
                return latest
            uncertain = self.store.update(request_id, status="unknown", error_code="submission_uncertain",
                                          message="Checking exchange for this request")
            self.reconcile_soon(request_id)
            return uncertain
        exchange_id = acknowledgement.get("order_id") if isinstance(acknowledgement, dict) else None
        if timings is not None:
            timings["exchange"] = (time.perf_counter() - exchange_started) * 1000
        if not exchange_id:
            latest = self.store.get(request_id)
            if latest["status"] != "submitting":
                return latest
            uncertain = self.store.update(request_id, status="unknown", error_code="missing_exchange_id",
                                          message="Checking exchange for this request")
            self.reconcile_soon(request_id)
            return uncertain
        try:
            record = self._apply_create_ack(request_id, acknowledgement)
        except Exception:
            record = self.store.update(request_id, status="unknown", error_code="invalid_exchange_ack",
                                       message="Checking exchange for this request")
        self.reconcile_soon(request_id)
        return record

    async def cancel_pair(self, pair_id: str):
        rows = self.store.pair(pair_id)
        if not rows:
            raise AppError(404, "not_found", "App pair not found")
        async def cancel_if_still_resting(row):
            try:
                await self.cancel(row["request_id"])
            except AppError as exc:
                if exc.code != "cancel_unavailable":
                    raise

        await asyncio.gather(*(cancel_if_still_resting(row) for row in rows
                               if row["status"] == "resting" and decimal(row["remaining_quantity"]) > 0))
        return self.store.pair(pair_id)

    def _apply_create_ack(self, request_id: str, acknowledgement: dict):
        current = self.store.get(request_id)
        exchange_id = acknowledgement["order_id"]
        if current["exchange_order_id"] and current["exchange_order_id"] != exchange_id:
            return self.store.update(request_id, status="unknown", error_code="order_identity_mismatch")
        if acknowledgement.get("client_order_id") not in {None, current["client_order_id"]}:
            return self.store.update(request_id, status="unknown", error_code="order_identity_mismatch")
        fields = {"exchange_order_id": exchange_id}
        try:
            filled = decimal(acknowledgement["fill_count"])
            remaining = decimal(acknowledgement["remaining_count"])
            if filled < 0 or remaining < 0 or filled + remaining > 1:
                raise ValueError("Invalid create quantities")
            filled = max(filled, decimal(current["filled_quantity"]))
            remaining = min(remaining, decimal(current["remaining_quantity"]))
            if filled + remaining > 1:
                raise ValueError("Conflicting create quantities")
            if current["status"] not in TERMINAL:
                fields.update(filled_quantity=canonical(filled), remaining_quantity=canonical(remaining))
                if filled == 1 and remaining == 0:
                    fields["status"] = "filled"
                elif remaining > 0:
                    fields["status"] = "resting" if current["status"] != "cancel_pending" else "cancel_pending"
                else:
                    fields["status"] = "unknown"
                fields["message"] = None
        except (KeyError, ValueError):
            # A successful create with an incomplete acknowledgment still has an exchange ID.
            # Keep it unresolved until the private stream or REST confirms its state.
            if current["status"] == "submitting":
                fields.update(status="unknown", message="Checking exchange order state")
        return self.store.update(request_id, **fields)

    async def cancel(self, request_id: str):
        record = self.store.get(request_id)
        if not record:
            raise AppError(404, "not_found", "App order not found")
        if record["status"] in TERMINAL or record["status"] == "cancel_pending":
            return record
        if record["status"] != "resting" or not record["exchange_order_id"] or decimal(record["remaining_quantity"]) <= 0:
            raise AppError(409, "cancel_unavailable", "Order state must be resolved before cancellation")
        record = self.store.update(request_id, status="cancel_pending", message="Cancel requested")
        try:
            await self.adapter.cancel_order(record)
        except Exception:
            record = self.store.update(request_id, message="Checking exchange after cancel request")
        self.reconcile_soon(request_id)
        return self.store.get(request_id)

    async def reconcile(self, record: dict):
        async with self.reconcile_lock:
            current = self.store.get(record["request_id"])
            if current["status"] in TERMINAL and current["average_fill_price"] is not None and current["actual_fees"] is not None:
                return current
            if current["exchange_order_id"]:
                raw = await self.adapter.get_order(current["exchange_order_id"])
            else:
                raw = await self.adapter.find_client_order(current["ticker"], current["client_order_id"])
                if raw is None:
                    latest = self.store.get(current["request_id"])
                    if latest["status"] in TERMINAL:
                        return latest
                    return self.store.update(current["request_id"], status="unknown",
                                             message="Checking exchange for the original client order ID")
            current = self.store.get(record["request_id"])
            updated = self._apply_raw(current, raw)
            exchange_id = updated["exchange_order_id"]
            fill = decimal(updated["filled_quantity"])
            if fill > 0 and exchange_id:
                try:
                    fills = await self.adapter.get_fills(exchange_id)
                    unique = {f["fill_id"]: f for f in fills if f.get("fill_id")}
                    total = sum((decimal(f["count_fp"]) for f in unique.values()), Decimal(0))
                    latest = self.store.get(record["request_id"])
                    if total == decimal(latest["filled_quantity"]) and total > 0:
                        field = "yes_price_dollars" if latest["outcome"] == "yes" else "no_price_dollars"
                        cost = sum((decimal(f[field]) * decimal(f["count_fp"]) for f in unique.values()), Decimal(0))
                        updated = self.store.update(record["request_id"], average_fill_price=canonical(cost / total))
                except Exception:
                    pass
            return self.store.get(record["request_id"])

    def _apply_raw(self, current: dict, raw: dict):
        if (raw.get("client_order_id") not in {None, current["client_order_id"]}
                or raw.get("ticker") != current["ticker"]):
            return self.store.update(current["request_id"], status="unknown", error_code="order_identity_mismatch")
        exchange_id = raw.get("order_id") or current["exchange_order_id"]
        fill = max(decimal(raw.get("fill_count_fp", raw.get("fill_count", "0"))), decimal(current["filled_quantity"]))
        remaining = min(decimal(raw.get("remaining_count_fp", raw.get("remaining_count", current["remaining_quantity"]))),
                        decimal(current["remaining_quantity"]))
        if fill < 0 or fill > 1 or remaining < 0 or fill + remaining > 1:
            return self.store.update(current["request_id"], status="unknown", error_code="invalid_exchange_quantities")
        raw_status = str(raw.get("status") or "").lower()
        if fill == 1 and remaining == 0 and raw_status in {"executed", "filled"}:
            status = "filled"
        elif raw_status in {"canceled", "cancelled"} and remaining == 0:
            status = "canceled"
        elif raw_status == "expired" and remaining == 0:
            status = "expired"
        elif raw_status == "resting" and remaining > 0:
            status = "resting" if current["status"] != "cancel_pending" else "cancel_pending"
        else:
            status = "unknown"
        if status == "unknown" and current["status"] in {"resting", "cancel_pending"} and remaining > 0:
            status = current["status"]
        if current["status"] in TERMINAL and not (status == "filled" and fill == 1):
            status = current["status"]
        canceled = current["canceled_quantity"]
        if status in {"canceled", "expired"}:
            canceled = canonical(max(Decimal(0), Decimal(1) - fill))
        if status == "filled":
            canceled = "0"
        fields = {
            "exchange_order_id": exchange_id, "raw_status": raw_status,
            "filled_quantity": canonical(fill), "remaining_quantity": canonical(remaining),
            "canceled_quantity": canceled, "status": status, "message": None,
        }
        if fill > 0 and raw.get("maker_fees_dollars") is not None and raw.get("taker_fees_dollars") is not None:
            try:
                fields["actual_fees"] = canonical(decimal(raw["maker_fees_dollars"]) + decimal(raw["taker_fees_dollars"]))
            except ValueError:
                pass
        return self.store.update(current["request_id"], **fields)

    async def on_private_order(self, raw: dict):
        market_ticker = raw.get("ticker")
        if not market_ticker:
            return
        current = self.store.find_owned(raw.get("client_order_id"), raw.get("order_id"), market_ticker)
        if current:
            self._apply_raw(current, raw)

    async def reconcile_all(self):
        failed = False
        for record in [*self.store.unresolved(), *self.store.pending_accounting()]:
            try:
                await self.reconcile(record)
            except Exception:
                failed = True
                continue
        self.recovering = failed or any(
            row["status"] in {"submitting", "unknown"} for row in self.store.unresolved()
        )
