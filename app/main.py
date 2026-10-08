import asyncio
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.books import BookRegistry
from app.config import Config
from app.kalshi import ExchangeError, KalshiAdapter
from app.markets import MarketService
from app.models import OrderRequest, PairedOrderRequest, PaperConfigureRequest, decimal, ticker
from app.orders import AppError, OrderService
from app.paper import FeeModel, PaperDesk, PaperPair
from app.store import Store
from app.stream import KalshiFeed


STATIC = Path(__file__).resolve().parent.parent / "static"


def public_order(row: dict) -> dict:
    return {key: value for key, value in row.items() if key not in {"account_key", "expires_unix"}}


def create_app(config=None, adapter=None) -> FastAPI:
    config = config or Config()
    sessions: dict[str, str] = {}
    viewers: set[asyncio.Event] = set()

    def notify_viewers():
        for changed in viewers:
            changed.set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.adapter = adapter or KalshiAdapter(config)
        app.state.stream_epoch = str(uuid.uuid4())
        app.state.books = BookRegistry()
        app.state.store = Store(config)
        app.state.feed = KalshiFeed(config, app.state.adapter, app.state.books, app.state.store)
        app.state.markets = MarketService(app.state.adapter, config, app.state.books, lambda: app.state.feed.healthy)
        app.state.orders = OrderService(config, app.state.adapter, app.state.markets, app.state.store, app.state.feed)
        app.state.paper = PaperDesk(app.state.books, app.state.markets, app.state.feed, app.state.store,
                                    config.environment, config.db_path.parent / "paper_evidence")
        app.state.feed.orders = app.state.orders
        app.state.feed.paper = app.state.paper
        app.state.books.on_change = notify_viewers
        def on_order_change():
            app.state.paper.refresh_owned()
            notify_viewers()
        app.state.store.on_change = on_order_change
        app.state.feed.on_change = notify_viewers
        app.state.markets.on_change = notify_viewers
        app.state.paper.on_change = notify_viewers
        if config.trading_enabled and hasattr(app.state.adapter, "check_trade_scope"):
            await app.state.adapter.check_trade_scope()
        await app.state.orders.startup_recovery()
        app.state.feed.start()

        async def poll():
            while True:
                await asyncio.sleep(config.reconcile_seconds)
                await app.state.orders.reconcile_all()

        task = asyncio.create_task(poll())
        async def refresh_paper_metadata():
            while True:
                await asyncio.sleep(config.metadata_ttl_seconds)
                pair = app.state.paper.config
                if not pair:
                    continue
                try:
                    metadata, fees = await prepare_paper((pair.primary_ticker, pair.other_ticker))
                except Exception:
                    app.state.paper.metadata = {}
                    app.state.paper.fees = {}
                    app.state.paper.evaluate("refresh", time.perf_counter_ns(), time.perf_counter_ns())
                    continue
                if pair != app.state.paper.config:
                    continue
                old_signature = [(app.state.paper.metadata.get(t, ({}, 0))[0].get(key), metadata[t][0].get(key))
                                 for t in (pair.primary_ticker, pair.other_ticker)
                                 for key in ("status", "price_ranges", "rules_primary", "rules_secondary")]
                if fees != app.state.paper.fees or any(old != new for old, new in old_signature):
                    was_observing = app.state.paper.observing
                    app.state.paper.configure(PaperPair(**(vars(pair) | {"version": pair.version + 1})), metadata, fees)
                    if was_observing:
                        app.state.paper.start()
                else:
                    app.state.paper.metadata, app.state.paper.fees = metadata, fees
                    app.state.paper.evaluate("refresh", time.perf_counter_ns(), time.perf_counter_ns())

        refresh_task = asyncio.create_task(refresh_paper_metadata())
        try:
            yield
        finally:
            task.cancel()
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(asyncio.CancelledError):
                await refresh_task
            await app.state.paper.close()
            await app.state.feed.stop()
            await app.state.orders.stop()
            app.state.store.close()
            await app.state.adapter.close()

    app = FastAPI(title="Kalshi one-contract tool", lifespan=lifespan)

    async def prepare_paper(tickers: tuple[str, str]):
        metadata, fees = {}, {}
        event_cache, series_cache = {}, {}
        for market_ticker in tickers:
            market = await app.state.adapter.get_market(market_ticker)
            event_ticker = market.get("event_ticker")
            if not event_ticker:
                raise ValueError("Market has no event ticker for fee lookup")
            if event_ticker not in event_cache:
                event_cache[event_ticker] = await app.state.adapter.get_event(event_ticker)
            event = event_cache[event_ticker]
            series_ticker = event.get("series_ticker")
            if not series_ticker:
                raise ValueError("Event has no series ticker for fee lookup")
            if series_ticker not in series_cache:
                series_cache[series_ticker] = await app.state.adapter.get_series(series_ticker)
            series = series_cache[series_ticker]
            override_type, override_multiplier = event.get("fee_type_override"), event.get("fee_multiplier_override")
            if (override_type is None) != (override_multiplier is None):
                fee_type, multiplier = "unsupported", "-1"
            else:
                fee_type = override_type if override_type is not None else series.get("fee_type", "unsupported")
                multiplier = override_multiplier if override_multiplier is not None else series.get("fee_multiplier", "-1")
            fees[market_ticker] = FeeModel(fee_type, decimal(multiplier),
                f"schedule:2026-07-07|series:{series.get('last_updated_ts')}|event:{event.get('last_updated_ts')}")
            metadata[market_ticker] = (market, time.monotonic())
        return metadata, fees

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.headers.get("host", "").split(":")[0].lower()
        if host not in {"127.0.0.1", "localhost"}:
            return JSONResponse({"code": "invalid_host", "message": "Use the local server address"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            expected = f"{request.url.scheme}://{request.headers.get('host')}"
            if request.headers.get("origin") != expected:
                return JSONResponse({"code": "invalid_origin", "message": "Same-origin request required"}, status_code=403)
            cookie = request.cookies.get("order_session")
            if not cookie or sessions.get(cookie) != request.headers.get("x-request-token"):
                return JSONResponse({"code": "invalid_token", "message": "Reload the page to refresh the request token"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        fields = {error["loc"][-1] for error in exc.errors() if error.get("loc")}
        code = "invalid_price" if "price" in fields else "invalid_ticker" if "ticker" in fields else "invalid_request"
        return JSONResponse({"code": code, "message": "Invalid request fields"}, status_code=422)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.css")
    async def css():
        return FileResponse(STATIC / "app.css", media_type="text/css")

    @app.get("/app.js")
    async def javascript():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/paper.js")
    async def paper_javascript():
        return FileResponse(STATIC / "paper.js", media_type="text/javascript")

    @app.get("/api/status")
    async def status(request: Request):
        session = request.cookies.get("order_session")
        if not session or session not in sessions:
            session = secrets.token_urlsafe(32)
            sessions[session] = secrets.token_urlsafe(32)
        response = JSONResponse({
            **state_status(),
            "request_token": sessions[session],
        })
        response.set_cookie("order_session", session, httponly=True, samesite="strict")
        return response

    def state_status():
        return {
            "environment": config.environment, "trading_enabled": config.trading_enabled,
            "authentication_ready": app.state.adapter.private_ready,
            "trade_scope_status": getattr(app.state.adapter, "trade_scope_status", "unknown"),
            "recovering": app.state.orders.recovering, "connected": app.state.feed.healthy,
            "feed_error": app.state.feed.last_error,
            "order_stream_ready": app.state.feed.order_stream_ready,
            "ui_stream_timeout_seconds": config.ui_stream_timeout_seconds,
            "feed_health_timeout_seconds": config.feed_health_timeout_seconds,
            "order_expiry_seconds": config.expiry_seconds,
        }

    @app.get("/api/markets/{market_ticker}/summary")
    async def market_summary(market_ticker: str):
        try:
            return await app.state.markets.summary(ticker(market_ticker))
        except ValueError as exc:
            raise AppError(422, "invalid_ticker", str(exc)) from exc
        except ExchangeError as exc:
            code = "invalid_ticker" if exc.status == 404 else "market_unavailable"
            raise AppError(404 if exc.status == 404 else 503, code, "Market could not be loaded") from exc
        except Exception as exc:
            raise AppError(503, "market_unavailable", "Market could not be loaded") from exc

    @app.get("/api/paper")
    async def paper_state():
        return app.state.paper.view()

    @app.post("/api/paper/configure")
    async def configure_paper(payload: PaperConfigureRequest):
        if payload.primary_ticker == payload.other_ticker:
            raise AppError(422, "same_market", "Paper arbitrage needs two distinct markets")
        try:
            metadata, fees = await prepare_paper((payload.primary_ticker, payload.other_ticker))
        except Exception as exc:
            raise AppError(503, "paper_metadata_unavailable", "Market or fee metadata could not be loaded") from exc
        prior = app.state.paper.config
        pair = PaperPair(payload.primary_ticker, payload.primary_outcome, payload.other_ticker,
                         payload.other_outcome, payload.relationship, decimal(payload.quantity),
                         decimal(payload.min_profit), decimal(payload.safety_margin),
                         payload.direct_account, payload.settlement_asserted,
                          prior.version + 1 if prior else 1, tuple(payload.delays_ms))
        app.state.paper.configure(pair, metadata, fees)
        app.state.feed.wake.set()
        return app.state.paper.view()

    @app.post("/api/paper/start")
    async def start_paper():
        try:
            app.state.paper.start()
        except ValueError as exc:
            raise AppError(422, "paper_not_configured", str(exc)) from exc
        return app.state.paper.view()

    @app.post("/api/paper/stop")
    async def stop_paper():
        app.state.paper.stop()
        return app.state.paper.view()

    @app.post("/api/paper/reset")
    async def reset_paper():
        app.state.paper.reset()
        return app.state.paper.view()

    @app.get("/api/events")
    async def events(request: Request, tickers: str = ""):
        try:
            watched = [ticker(value) for value in tickers.split(",") if value.strip()]
        except ValueError as exc:
            raise AppError(422, "invalid_ticker", str(exc)) from exc
        unique = list(dict.fromkeys(watched))
        if len(unique) > 2 or len(watched) > 2:
            raise AppError(422, "too_many_tickers", "Watch at most two markets")
        try:
            viewer_id = app.state.feed.register(set(unique))
        except ValueError as exc:
            raise AppError(503, "stream_busy", str(exc)) from exc

        async def states():
            revision = 0
            prior = None
            last_sent = 0.0
            changed = asyncio.Event()
            viewers.add(changed)
            try:
                while not await request.is_disconnected():
                    app.state.feed.renew(viewer_id)
                    changed.clear()
                    current = (app.state.books.revision, app.state.store.revision,
                               app.state.feed.revision, app.state.markets.revision)
                    elapsed = time.monotonic() - last_sent
                    if current != prior and prior is not None and elapsed < config.ui_update_interval_ms / 1000:
                        await asyncio.sleep(config.ui_update_interval_ms / 1000 - elapsed)
                        continue
                    if current != prior or elapsed >= 1:
                        markets = [app.state.markets.summary_cached(value) for value in unique]
                        revision += 1
                        envelope = {"schema_version": 2, "stream_epoch": app.state.stream_epoch,
                                    "revision": revision, "server_time": datetime.now(timezone.utc).isoformat(),
                                    "status": state_status(), "markets": markets,
                                    "orders": [public_order(row) for row in app.state.store.list()]}
                        yield f"event: state\ndata: {json.dumps(envelope)}\n\n"
                        prior, last_sent = current, time.monotonic()
                    try:
                        await asyncio.wait_for(changed.wait(), timeout=max(0.01, 1 - (time.monotonic() - last_sent)))
                    except asyncio.TimeoutError:
                        pass
            finally:
                viewers.discard(changed)
                app.state.feed.release(viewer_id)

        return StreamingResponse(states(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.get("/api/orders")
    async def list_orders():
        return [public_order(row) for row in app.state.store.list()]

    @app.get("/api/orders/by-request/{request_id}")
    async def order_by_request(request_id: UUID):
        row = app.state.store.get(str(request_id))
        if not row:
            raise AppError(404, "not_found", "App order not found")
        if row["status"] in {"filled", "canceled", "expired"} and row["filled_quantity"] != "0" and (
            row["average_fill_price"] is None or row["actual_fees"] is None
        ):
            try:
                row = await app.state.orders.reconcile(row)
            except Exception:
                pass
        return public_order(row)

    @app.post("/api/orders")
    async def post_order(payload: OrderRequest):
        timings = {}
        row, created = await app.state.orders.submit(payload, timings)
        response = JSONResponse(public_order(row), status_code=201 if created else 200)
        if timings:
            response.headers["Server-Timing"] = ", ".join(
                f"{name};dur={duration:.1f}" for name, duration in timings.items()
            )
        return response

    @app.post("/api/orders/{request_id}/cancel")
    async def cancel_order(request_id: UUID):
        row = await app.state.orders.cancel(str(request_id))
        return public_order(row)

    @app.get("/api/pairs/{pair_id}")
    async def pair_by_id(pair_id: UUID):
        rows = app.state.store.pair(str(pair_id))
        if not rows:
            raise AppError(404, "not_found", "App pair not found")
        return [public_order(row) for row in rows]

    @app.post("/api/pairs")
    async def post_pair(payload: PairedOrderRequest):
        timings = {}
        rows, created = await app.state.orders.submit_pair(payload, timings)
        response = JSONResponse([public_order(row) for row in rows], status_code=201 if created else 200)
        if timings:
            response.headers["Server-Timing"] = ", ".join(
                f"{name};dur={duration:.1f}" for name, duration in timings.items()
            )
        return response

    @app.post("/api/pairs/{pair_id}/cancel")
    async def cancel_pair(pair_id: UUID):
        rows = await app.state.orders.cancel_pair(str(pair_id))
        return [public_order(row) for row in rows]

    return app


app = create_app()
