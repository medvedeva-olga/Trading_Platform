"""Application settings, market constants, money helpers and JWT utilities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import jwt
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Supported instruments and their starting prices (USDT quote currency).
TICKERS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT")

INITIAL_PRICES: dict[str, Decimal] = {
    "BTCUSDT": Decimal("65000.00000000"),
    "ETHUSDT": Decimal("3500.00000000"),
    "SOLUSDT": Decimal("150.00000000"),
    "TONUSDT": Decimal("6.50000000"),
}

# Relative price change per tick: calm / normal / high volatility regimes.
VOLATILITY_PRESETS: dict[str, str] = {
    "calm": "0.0005",
    "normal": "0.0020",
    "high": "0.0100",
}

MONEY_QUANTUM = Decimal("0.00000001")
ZERO = Decimal("0.00000000")


class Settings(BaseSettings):
    """Environment driven settings. Every variable is prefixed with ``TRADING_``."""

    model_config = SettingsConfigDict(
        env_prefix="TRADING_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Copy Trading Mock Exchange"
    environment: str = "test"
    debug: bool = False

    host: str = "0.0.0.0"
    port: int = 8000

    # SQLite file is intentionally on disk so tests can inspect it directly.
    database_url: str = "sqlite+aiosqlite:///./trading.db"
    database_path: Path = Path("trading.db")

    jwt_secret: str = "change-me-in-production-qa-only-secret"
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 3600

    market_tick_interval_ms: int = 500
    market_regime: Literal["calm", "normal", "high"] = "normal"
    market_volatility: Decimal | None = Field(
        default=None,
        description="Override of the relative random-walk step; takes precedence over market_regime.",
    )
    market_seed: int | None = Field(
        default=None, description="Optional RNG seed for reproducible price series."
    )
    spread_bps: int = 10  # 1 bp = 0.01%

    # Matching engine settings. The synthetic book is deterministic background
    # liquidity; QA can override it through the guarded test endpoint.
    fee_rate: Decimal = Decimal("0.00100000")  # 0.1% charged to the taker
    synthetic_depth_qty: Decimal = Decimal("1000.00000000")
    depth_levels: int = 5
    qa_mode: bool = False

    min_deposit: Decimal = Decimal("100.00000000")
    max_deposit: Decimal = Decimal("1000000.00000000")
    min_order_amount: Decimal = Decimal("0.00000001")

    # Emulated network latency between a Master fill and copy execution. Set
    # TRADING_COPY_DELAY_MS=1500 (larger than a tick) to deterministically test
    # SLIPPAGE_EXCEEDED with max_slippage_percent close to zero.
    copy_delay_ms: int = 250

    @property
    def volatility(self) -> Decimal:
        if self.market_volatility is not None:
            return self.market_volatility
        return Decimal(VOLATILITY_PRESETS[self.market_regime])


settings = Settings()


def money(value: Any) -> Decimal:
    """Convert any acceptable numeric input to an 8-decimal ``Decimal``."""
    return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def create_access_token(user_id: int, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate signature/expiration. Raises ``ValueError`` on failure."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:
        raise ValueError("Invalid or expired JWT") from exc
    if "sub" not in payload:
        raise ValueError("JWT is missing the subject claim")
    return payload
