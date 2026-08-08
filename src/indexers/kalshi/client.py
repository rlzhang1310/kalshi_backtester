from collections.abc import Generator
from typing import Optional

from src.common.client import HttpClient
from src.indexers.kalshi.models import Market, Trade

KALSHI_API_HOST = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiClient:
    def __init__(self, host: str = KALSHI_API_HOST):
        self.host = host
        self.http = HttpClient(base_url=host, rate_limit=10)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def close(self):
        self.http.close()

    def get_market(self, ticker: str, historical: bool = False) -> Market:
        market_path = "/historical/markets" if historical else "/markets"
        data = self.http.get(f"{market_path}/{ticker.upper()}")
        return Market.from_dict(data["market"])

    def get_event_markets(
        self,
        event_ticker: str,
        historical: bool = False,
        limit: int = 1000,
    ) -> list[Market]:
        """Return every market belonging to one event.

        Kalshi partitions settled markets between the regular and historical
        endpoints. Callers that need a complete event should query this method
        once with each value of ``historical`` and de-duplicate the results.
        """
        endpoint = "/historical/markets" if historical else "/markets"
        all_markets: list[Market] = []
        cursor = None
        seen_cursors: set[str] = set()

        while True:
            params = {
                "event_ticker": event_ticker.upper(),
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor

            data = self.http.get(endpoint, params=params)
            all_markets.extend(
                Market.from_dict(market) for market in data.get("markets", [])
            )

            next_cursor = data.get("cursor")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(
                    f"Kalshi returned a repeated cursor while listing {event_ticker}"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return all_markets

    def get_event_milestones(
        self,
        event_ticker: str,
        limit: int = 500,
    ) -> list[dict]:
        """Return sports milestones linked to an event ticker."""
        all_milestones: list[dict] = []
        cursor = None
        seen_cursors: set[str] = set()

        while True:
            params = {
                "related_event_ticker": event_ticker.upper(),
                "category": "Sports",
                "limit": limit,
            }
            if cursor:
                params["cursor"] = cursor

            data = self.http.get("/milestones", params=params)
            all_milestones.extend(data.get("milestones", []))

            next_cursor = data.get("cursor")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(
                    "Kalshi returned a repeated cursor while listing milestones "
                    f"for {event_ticker}"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return all_milestones

    def get_market_trades(
        self,
        ticker: str,
        limit: int = 1000,
        verbose: bool = True,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        historical: bool = False,
    ) -> list[Trade]:
        raw_trades = self.get_market_trades_data(
            ticker=ticker,
            limit=limit,
            verbose=verbose,
            min_ts=min_ts,
            max_ts=max_ts,
            historical=historical,
        )
        return [Trade.from_dict(trade) for trade in raw_trades]

    def get_market_trades_data(
        self,
        ticker: str,
        limit: int = 1000,
        verbose: bool = True,
        min_ts: Optional[int] = None,
        max_ts: Optional[int] = None,
        historical: bool = False,
    ) -> list[dict]:
        """Return raw public trades for one market, following every page."""
        all_trades: list[dict] = []
        cursor = None
        seen_cursors: set[str] = set()
        endpoint = "/historical/trades" if historical else "/markets/trades"

        while True:
            params = {"ticker": ticker, "limit": limit}
            if cursor:
                params["cursor"] = cursor
            if min_ts is not None:
                params["min_ts"] = min_ts
            if max_ts is not None:
                params["max_ts"] = max_ts

            data = self.http.get(endpoint, params=params)

            trades = data.get("trades", [])
            if trades:
                all_trades.extend(trades)
                if verbose:
                    print(f"Fetched {len(trades)} trades (total: {len(all_trades)})")

            next_cursor = data.get("cursor")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError(
                    f"Kalshi returned a repeated cursor while fetching {ticker}"
                )
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return all_trades

    def list_markets(self, limit: int = 20, **kwargs) -> list[Market]:
        params = {"limit": limit, **kwargs}
        data = self.http.get("/markets", params=params)
        return [Market.from_dict(m) for m in data.get("markets", [])]

    def list_all_markets(self, limit: int = 200) -> list[Market]:
        all_markets = []
        cursor = None

        while True:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor

            data = self.http.get("/markets", params=params)

            markets = [Market.from_dict(m) for m in data.get("markets", [])]
            if markets:
                all_markets.extend(markets)
                print(f"Fetched {len(markets)} markets (total: {len(all_markets)})")

            cursor = data.get("cursor")
            if not cursor:
                break

        return all_markets

    def iter_markets(
        self,
        limit: int = 200,
        cursor: Optional[str] = None,
        min_close_ts: Optional[int] = None,
        max_close_ts: Optional[int] = None,
    ) -> Generator[tuple[list[Market], Optional[str]], None, None]:
        while True:
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            if min_close_ts is not None:
                params["min_close_ts"] = min_close_ts
            if max_close_ts is not None:
                params["max_close_ts"] = max_close_ts

            data = self.http.get("/markets", params=params)

            markets = [Market.from_dict(m) for m in data.get("markets", [])]
            cursor = data.get("cursor")

            yield markets, cursor

            if not cursor:
                break

    def get_recent_trades(self, limit: int = 100) -> list[Trade]:
        data = self.http.get("/markets/trades", params={"limit": limit})
        return [Trade.from_dict(t) for t in data.get("trades", [])]
