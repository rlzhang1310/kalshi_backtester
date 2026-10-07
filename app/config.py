import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Config:
    environment: str = os.getenv("KALSHI_ENV", "demo").lower()
    trading_enabled: bool = os.getenv("KALSHI_TRADING_ENABLED", "false").lower() == "true"
    api_key_id: str = os.getenv("KALSHI_API_KEY_ID", "")
    private_key_path: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    db_path: Path = Path(os.getenv("APP_DB_PATH", str(ROOT / "data" / "one_contract_orders.sqlite3")))
    expiry_seconds: int = int(os.getenv("ORDER_EXPIRY_SECONDS", "300"))
    reconcile_seconds: float = float(os.getenv("RECONCILE_INTERVAL_SECONDS", "15"))
    metadata_ttl_seconds: int = int(os.getenv("METADATA_TTL_SECONDS", "30"))
    ui_update_interval_ms: int = int(os.getenv("UI_UPDATE_INTERVAL_MS", "33"))
    ui_stream_timeout_seconds: int = int(os.getenv("UI_STREAM_TIMEOUT_SECONDS", "5"))
    feed_health_timeout_seconds: int = int(os.getenv("FEED_HEALTH_TIMEOUT_SECONDS", "15"))

    def __post_init__(self) -> None:
        if self.environment not in {"demo", "production"}:
            raise ValueError("KALSHI_ENV must be demo or production")
        if (self.expiry_seconds < 30 or self.reconcile_seconds <= 0 or self.metadata_ttl_seconds <= 0
            or self.ui_update_interval_ms < 25 or self.ui_stream_timeout_seconds <= 0
            or self.feed_health_timeout_seconds <= 0):
            raise ValueError("Invalid order or stream setting")
        if self.trading_enabled and (not self.api_key_id or not self.private_key_path):
            raise ValueError("Trading needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")

    @property
    def base_url(self) -> str:
        host = "external-api.demo.kalshi.co" if self.environment == "demo" else "external-api.kalshi.com"
        return f"https://{host}/trade-api/v2"

    @property
    def ws_url(self) -> str:
        host = "external-api-ws.demo.kalshi.co" if self.environment == "demo" else "external-api-ws.kalshi.com"
        return f"wss://{host}/trade-api/ws/v2"

    @property
    def account_key(self) -> str:
        # A credential change must not inherit the previous key's local orders.
        return self.api_key_id or "public-read-only"
