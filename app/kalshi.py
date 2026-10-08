import base64
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class ExchangeError(Exception):
    def __init__(self, status: int, code: str = "exchange_error"):
        super().__init__(code)
        self.status = status
        self.code = code


class KalshiAdapter:
    def __init__(self, config):
        self.config = config
        self.http = httpx.AsyncClient(base_url=config.base_url, timeout=httpx.Timeout(8.0), limits=httpx.Limits(max_connections=10))
        self.key = None
        self.last_success = 0.0
        self.auth_failed = False
        self.trade_scope_status = "unknown"
        self.read_cooldown_until = 0.0
        self.read_backoff_seconds = 2.0
        if config.api_key_id and config.private_key_path:
            key = serialization.load_pem_private_key(Path(config.private_key_path).read_bytes(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey):
                raise ValueError("Only RSA API private keys are supported")
            self.key = key

    async def close(self):
        await self.http.aclose()

    @property
    def private_ready(self) -> bool:
        return bool(self.key and self.config.api_key_id and not self.auth_failed)

    async def check_trade_scope(self) -> str:
        """Check the configured key's trading permission without submitting an order."""
        self.trade_scope_status = "unknown"
        if not self.private_ready:
            return self.trade_scope_status
        try:
            data = await self._request("GET", "/api_keys", private=True)
        except (ExchangeError, httpx.HTTPError):
            return self.trade_scope_status
        keys = data.get("api_keys", []) if isinstance(data, dict) else []
        own_key = next((key for key in keys if isinstance(key, dict) and
                        key.get("api_key_id") == self.config.api_key_id), None)
        if own_key is None or not isinstance(own_key.get("scopes"), list):
            return self.trade_scope_status
        if own_key.get("subaccount") not in (None, 0):
            self.trade_scope_status = "wrong_subaccount"
        elif {"write", "write::trade"}.intersection(own_key["scopes"]):
            self.trade_scope_status = "allowed"
        else:
            self.trade_scope_status = "read_only"
        return self.trade_scope_status

    def _headers(self, method: str, path: str) -> dict:
        return self.sign_headers(method, "/trade-api/v2" + path)

    def sign_headers(self, method: str, full_path: str) -> dict:
        if self.key is None:
            raise ExchangeError(401, "credentials_missing")
        stamp = str(int(time.time() * 1000))
        signed = f"{stamp}{method}{full_path}".encode()
        signature = self.key.sign(signed, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.config.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": stamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    async def _request(self, method: str, path: str, *, private: bool = False, **kwargs) -> dict:
        if method == "GET" and time.monotonic() < self.read_cooldown_until:
            raise ExchangeError(429, "rate_limited")
        headers = self._headers(method, path) if private else None
        response = await self.http.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            if method == "GET" and response.status_code == 429:
                self.read_cooldown_until = time.monotonic() + self.read_backoff_seconds
                self.read_backoff_seconds = min(30.0, self.read_backoff_seconds * 2)
            if private and response.status_code == 401:
                self.auth_failed = True
            try:
                body = response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                code = (body.get("code") if isinstance(body, dict) else None) or \
                    (error.get("code") if isinstance(error, dict) else None)
            except (ValueError, AttributeError):
                code = None
            code = code or f"exchange_http_{response.status_code}"
            raise ExchangeError(response.status_code, str(code)[:80])
        self.last_success = time.monotonic()
        if method == "GET":
            self.read_backoff_seconds = 2.0
        if private:
            self.auth_failed = False
        return response.json()

    async def get_market(self, ticker: str) -> dict:
        return (await self._request("GET", f"/markets/{quote(ticker, safe='')}"))["market"]

    async def get_event(self, event_ticker: str) -> dict:
        return (await self._request("GET", f"/events/{quote(event_ticker, safe='')}"))["event"]

    async def get_series(self, series_ticker: str) -> dict:
        return (await self._request("GET", f"/series/{quote(series_ticker, safe='')}"))["series"]

    async def get_orderbook(self, ticker: str) -> dict:
        return await self._request("GET", f"/markets/{quote(ticker, safe='')}/orderbook")

    async def create_order(self, order: dict) -> dict:
        from app.models import canonical, decimal

        yes_price = decimal(order["limit_price"]) if order["outcome"] == "yes" else 1 - decimal(order["limit_price"])
        payload = {
            "ticker": order["ticker"], "client_order_id": order["client_order_id"],
            "side": "bid" if order["outcome"] == "yes" else "ask",
            "count": "1", "price": canonical(yes_price),
            "time_in_force": "good_till_canceled", "expiration_time": int(order["expires_unix"]),
            "self_trade_prevention_type": "taker_at_cross", "post_only": True,
            "cancel_order_on_pause": True, "reduce_only": False, "subaccount": 0,
            "exchange_index": -1,
        }
        return await self._request("POST", "/portfolio/events/orders", private=True, json=payload)

    async def get_order(self, exchange_order_id: str) -> dict:
        return (await self._request("GET", f"/portfolio/orders/{quote(exchange_order_id, safe='')}", private=True))["order"]

    async def find_client_order(self, ticker: str, client_id: str) -> dict | None:
        cursor = None
        seen = set()
        while True:
            params = {"ticker": ticker, "subaccount": 0, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/orders", private=True, params=params)
            for order in data.get("orders", []):
                if order.get("client_order_id") == client_id:
                    return order
            cursor = data.get("cursor")
            if not cursor:
                return None
            if cursor in seen:
                raise ExchangeError(502, "repeated_cursor")
            seen.add(cursor)

    async def cancel_order(self, order: dict) -> dict:
        return await self._request(
            "DELETE", f"/portfolio/events/orders/{quote(order['exchange_order_id'], safe='')}",
            private=True, params={"market_ticker": order["ticker"], "subaccount": 0, "exchange_index": -1}
        )

    async def get_fills(self, exchange_order_id: str) -> list[dict]:
        cursor = None
        seen = set()
        fills = []
        while True:
            params = {"order_id": exchange_order_id, "subaccount": 0, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/portfolio/fills", private=True, params=params)
            fills.extend(fill for fill in data.get("fills", []) if fill.get("order_id") == exchange_order_id)
            cursor = data.get("cursor")
            if not cursor:
                return fills
            if cursor in seen:
                raise ExchangeError(502, "repeated_cursor")
            seen.add(cursor)
