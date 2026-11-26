"""Pydantic v2 request/response schemas and WebSocket event contracts.

Financial values are ``Decimal`` and are serialized to JSON as fixed 8-decimal
strings, e.g. ``"65000.00000000"``, so QA assertions stay exact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

from .config import MONEY_QUANTUM

MASTER_ID_IMMUTABLE_MESSAGE = (
    "master_id is immutable for an existing subscription. "
    "Use POST /api/v1/copy-trading/subscribe to create a new subscription "
    "for another master."
)


def serialize_money(value: Decimal) -> str:
    # ``format(..., 'f')`` forces fixed-point notation: SQLite/Decimal can
    # otherwise produce scientific notation such as ``"0E-8"``.
    return format(value.quantize(MONEY_QUANTUM), "f")


def serialize_datetime(value: datetime) -> str:
    # SQLite drops tzinfo on read; treat naive values as UTC (we always store UTC).
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


Money = Annotated[
    Decimal, PlainSerializer(serialize_money, return_type=str, when_used="json")
]
UtcTimestamp = Annotated[
    datetime, PlainSerializer(serialize_datetime, return_type=str, when_used="json")
]

Role = Literal["MASTER", "SLAVE"]
Side = Literal["BUY", "SELL"]
TimeInForce = Literal["IOC", "GTC"]
OrderType = Literal["LIMIT", "MARKET"]
Ticker = Literal["BTCUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT"]
# PENDING is kept as a deprecated alias for clients of the pre-book API.
OrderStatus = Literal["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "PENDING"]
CopyMode = Literal["PROPORTIONAL", "FIXED_RATIO"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BusinessError(Exception):
    """Domain error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "detail": {
                        "code": "INSUFFICIENT_FUNDS",
                        "message": "Not enough free balance to reserve the order.",
                        "details": {
                            "required": "1000.00000000",
                            "available": "100.00000000",
                        },
                    }
                },
                {"detail": "Could not validate credentials"},
            ]
        }
    )

    detail: Any = Field(
        description="Either a plain message (4xx auth errors) or a structured business error."
    )


# ---------------------------------------------------------------------------
# Auth & users
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "username": "qa_master_001",
                    "password": "StrongPass123!",
                    "role": "MASTER",
                },
                {
                    "username": "qa_slave_001",
                    "password": "StrongPass123!",
                    "role": "SLAVE",
                },
            ]
        }
    )

    username: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[A-Za-z0-9_\-]+$",
        examples=["qa_master_001"],
        description="Unique login used by tests to isolate fixtures.",
    )
    password: str = Field(
        min_length=8,
        max_length=72,
        examples=["StrongPass123!"],
        description="Plain password; stored as a bcrypt hash.",
    )
    role: Role = Field(
        examples=["MASTER"],
        description="`MASTER` opens positions, `SLAVE` copies them.",
    )


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"username": "qa_master_001", "password": "StrongPass123!"}]
        }
    )

    username: str = Field(examples=["qa_master_001"])
    password: str = Field(examples=["StrongPass123!"])


class TokenResponse(BaseModel):
    access_token: str = Field(
        description="JWT (HS256) to send as `Authorization: Bearer <token>`."
    )
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds.")
    user_id: int
    role: Role

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "user_id": 1,
                    "role": "MASTER",
                }
            ]
        }
    )


class UserResponse(ORMModel):
    id: int
    username: str
    role: str
    created_at: UtcTimestamp


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
class DepositRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"amount": "10000.00000000"}]}
    )

    amount: Money = Field(
        gt=0,
        examples=["10000.00000000"],
        description="Deposit size in USDT, between 100 and 1 000 000.",
    )


class DepositResponse(BaseModel):
    transaction_id: int
    cash_balance: Money
    frozen_balance: Money

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transaction_id": 1,
                    "cash_balance": "10000.00000000",
                    "frozen_balance": "0.00000000",
                }
            ]
        }
    )


class PositionSchema(ORMModel):
    ticker: str
    quantity: Money
    avg_entry_price: Money
    mark_price: Money = Field(description="Bid price used for valuation.")
    market_value: Money
    unrealized_pnl: Money
    stop_loss: Money | None
    take_profit: Money | None


class PortfolioSummary(BaseModel):
    user_id: int
    username: str
    role: str
    cash_balance: Money = Field(description="Free USDT available for new orders.")
    frozen_balance: Money = Field(description="USDT reserved by pending GTC orders.")
    total_balance: Money = Field(description="`cash_balance + frozen_balance`.")
    positions: list[PositionSchema]
    positions_market_value: Money
    unrealized_pnl: Money
    realized_pnl: Money
    equity: Money = Field(description="`total_balance + positions_market_value`.")

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "username": "qa_master_001",
                    "role": "MASTER",
                    "cash_balance": "3500.00000000",
                    "frozen_balance": "0.00000000",
                    "total_balance": "3500.00000000",
                    "positions": [
                        {
                            "ticker": "BTCUSDT",
                            "quantity": "0.10000000",
                            "avg_entry_price": "65000.00000000",
                            "mark_price": "65100.00000000",
                            "market_value": "6510.00000000",
                            "unrealized_pnl": "10.00000000",
                            "stop_loss": "62000.00000000",
                            "take_profit": "70000.00000000",
                        }
                    ],
                    "positions_market_value": "6510.00000000",
                    "unrealized_pnl": "10.00000000",
                    "realized_pnl": "0.00000000",
                    "equity": "10010.00000000",
                }
            ]
        }
    )


# ---------------------------------------------------------------------------
# Exposure (risk view)
# ---------------------------------------------------------------------------
class ExposurePosition(BaseModel):
    ticker: str
    quantity: Money
    mark_price: Money = Field(description="Bid price used for valuation.")
    exposure: Money = Field(description="`quantity * mark_price`.")


class AccountExposure(BaseModel):
    user_id: int
    total_exposure: Money = Field(
        description="Sum of `quantity * mark_price` over open positions."
    )
    positions: list[ExposurePosition]


class ExposureSnapshotEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "exposure_snapshot",
                    "user_id": 1,
                    "total_exposure": "0.00000000",
                    "positions": [],
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["exposure_snapshot"] = "exposure_snapshot"
    user_id: int
    total_exposure: Money
    positions: list[ExposurePosition]
    timestamp: UtcTimestamp


class ExposureUpdatedEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "exposure_updated",
                    "user_id": 1,
                    "total_exposure": "6500.00000000",
                    "delta": "6500.00000000",
                    "last_order_id": 42,
                    "reason": None,
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["exposure_updated"] = "exposure_updated"
    user_id: int
    total_exposure: Money
    delta: Money = Field(description="`+qty*price` for BUY, `-qty*price` for SELL.")
    last_order_id: int | None = None
    reason: str | None = None
    timestamp: UtcTimestamp


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
class TickerQuote(BaseModel):
    ticker: Ticker
    bid: Money
    ask: Money
    last: Money
    spread: Money
    timestamp: UtcTimestamp

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "ticker": "BTCUSDT",
                    "bid": "64996.75000000",
                    "ask": "65003.25000000",
                    "last": "65000.00000000",
                    "spread": "6.50000000",
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )


class DepthResponse(BaseModel):
    ticker: Ticker
    bids: list[tuple[Money, Money]]
    asks: list[tuple[Money, Money]]
    timestamp: UtcTimestamp


class DepthOverride(BaseModel):
    """QA-only replacement for the deterministic synthetic book."""

    ticker: Ticker
    bids: list[tuple[Money, Money]]
    asks: list[tuple[Money, Money]]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
class OrderCreateRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "ticker": "BTCUSDT",
                    "side": "BUY",
                    "amount": "0.10000000",
                    "min_price": "64000.00000000",
                    "max_price": "66000.00000000",
                    "time_in_force": "GTC",
                    "stop_loss": "62000.00000000",
                    "take_profit": "70000.00000000",
                },
                {
                    "ticker": "ETHUSDT",
                    "side": "SELL",
                    "amount": "1.00000000",
                    "min_price": "3400.00000000",
                    "max_price": "3600.00000000",
                    "time_in_force": "IOC",
                    "stop_loss": None,
                    "take_profit": None,
                },
            ]
        }
    )

    ticker: Ticker = Field(description="One of BTCUSDT, ETHUSDT, SOLUSDT, TONUSDT.")
    side: Side = Field(
        description="`BUY` opens/increases a long position, `SELL` reduces/closes it."
    )
    order_type: OrderType = Field(
        default="LIMIT",
        description=(
            "`LIMIT` respects the corridor and accepts `IOC`/`GTC`; `MARKET` matches any "
            "available price and requires `IOC` (`MARKET` + `GTC` returns 400)."
        ),
    )
    amount: Money = Field(
        gt=0, examples=["0.10000000"], description="Amount in base asset."
    )
    min_price: Money = Field(
        gt=0,
        examples=["64000.00000000"],
        description="Lower bound of the execution corridor.",
    )
    max_price: Money = Field(
        gt=0,
        examples=["66000.00000000"],
        description="Upper bound of the execution corridor.",
    )
    time_in_force: TimeInForce = Field(
        examples=["GTC"],
        description=(
            "`IOC`: fill now or cancel; `GTC`: wait for the price to enter the corridor "
            "(LIMIT orders only)."
        ),
    )
    stop_loss: Money | None = Field(default=None, gt=0, examples=["62000.00000000"])
    take_profit: Money | None = Field(default=None, gt=0, examples=["70000.00000000"])

    @model_validator(mode="after")
    def validate_corridor(self) -> OrderCreateRequest:
        if self.min_price > self.max_price:
            raise ValueError("min_price must be less than or equal to max_price")
        if (
            self.stop_loss is not None
            and self.take_profit is not None
            and self.stop_loss >= self.take_profit
        ):
            raise ValueError("stop_loss must be less than take_profit")
        return self


class OrderResponse(ORMModel):
    id: int
    user_id: int
    ticker: str
    side: str
    order_type: str
    amount: Money
    filled_qty: Money
    remaining_qty: Money
    min_price: Money
    max_price: Money
    time_in_force: str
    stop_loss: Money | None
    take_profit: Money | None
    status: str
    filled_price: Money | None
    reserved_amount: Money
    fee_amount: Money
    price_improvement: Money
    reason: str | None
    is_copy: bool
    copy_of_order_id: int | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    filled_at: UtcTimestamp | None


# ---------------------------------------------------------------------------
# Copy trading
# ---------------------------------------------------------------------------
class CopySubscribeRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "master_id": 1,
                    "allocation_usdt": "1000.00000000",
                    "copy_mode": "FIXED_RATIO",
                    "fixed_ratio": "0.10000000",
                    "max_slippage_percent": "1.00000000",
                },
                {
                    "master_id": 1,
                    "allocation_usdt": "2000.00000000",
                    "copy_mode": "PROPORTIONAL",
                    "fixed_ratio": None,
                    "max_slippage_percent": "0.50000000",
                },
            ]
        }
    )

    master_id: int = Field(
        gt=0, examples=[1], description="ID of the MASTER user to copy."
    )
    allocation_usdt: Money = Field(
        gt=0, examples=["1000.00000000"], description="Budget reserved for copying."
    )
    copy_mode: CopyMode = Field(
        examples=["FIXED_RATIO"],
        description=(
            "`FIXED_RATIO` uses `fixed_ratio` of the Master amount; "
            "`PROPORTIONAL` uses `allocation_usdt / master_equity`."
        ),
    )
    fixed_ratio: Money | None = Field(
        default=None,
        gt=0,
        examples=["0.10000000"],
        description="Required for FIXED_RATIO, ignored for PROPORTIONAL.",
    )
    max_slippage_percent: Money = Field(
        ge=0,
        le=100,
        examples=["1.00000000"],
        description="Max allowed deviation of the copy price from the Master execution price, in percent.",
    )

    @model_validator(mode="after")
    def validate_mode(self) -> CopySubscribeRequest:
        if self.copy_mode == "FIXED_RATIO" and self.fixed_ratio is None:
            raise ValueError("fixed_ratio is required when copy_mode is FIXED_RATIO")
        if self.fixed_ratio is not None and self.fixed_ratio > 1:
            raise ValueError("fixed_ratio must be <= 1")
        return self


class CopySubscriptionUpdate(BaseModel):
    """Partial update of an existing subscription's copying parameters.

    ``master_id`` and ``slave_id`` are intentionally not updatable: changing the
    master must create a new subscription so the previous audit record survives.
    Unknown fields (including ``master_id``) are rejected with 422.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def reject_master_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and "master_id" in data:
            raise BusinessError(
                "MASTER_ID_IMMUTABLE",
                MASTER_ID_IMMUTABLE_MESSAGE,
                400,
            )
        return data

    allocation_usdt: Money | None = Field(
        default=None,
        gt=0,
        examples=["1000.00000000"],
        description="New total budget reserved for copying.",
    )
    copy_mode: CopyMode | None = Field(
        default=None,
        examples=["FIXED_RATIO"],
        description="New copy mode; FIXED_RATIO requires fixed_ratio.",
    )
    fixed_ratio: Money | None = Field(
        default=None,
        gt=0,
        le=1,
        examples=["0.10000000"],
        description="New fixed share of the Master order amount.",
    )
    max_slippage_percent: Money | None = Field(
        default=None,
        ge=0,
        le=100,
        examples=["1.00000000"],
        description="New maximum allowed price deviation, in percent.",
    )

    @model_validator(mode="after")
    def validate_mode(self) -> CopySubscriptionUpdate:
        if self.copy_mode == "FIXED_RATIO" and self.fixed_ratio is None:
            raise ValueError("fixed_ratio is required when copy_mode is FIXED_RATIO")
        return self


class SubscriptionResponse(ORMModel):
    id: int
    slave_id: int
    master_id: int
    allocation_usdt: Money
    copy_mode: str
    fixed_ratio: Money | None
    max_slippage_percent: Money
    active: bool
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class MasterStats(BaseModel):
    user_id: int
    username: str
    equity: Money
    total_deposits: Money
    realized_pnl: Money
    unrealized_pnl: Money
    trades_count: int
    subscribers_count: int
    roi_percent: Money

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user_id": 1,
                    "username": "qa_master_001",
                    "equity": "10250.00000000",
                    "total_deposits": "10000.00000000",
                    "realized_pnl": "250.00000000",
                    "unrealized_pnl": "0.00000000",
                    "trades_count": 4,
                    "subscribers_count": 1,
                    "roi_percent": "2.50000000",
                }
            ]
        }
    )


class CopyHistoryItem(ORMModel):
    id: int
    subscription_id: int | None
    master_id: int
    slave_id: int
    master_order_id: int
    slave_order_id: int | None
    status: str
    skip_reason: str | None
    ticker: str
    amount: Money
    master_price: Money
    copy_price: Money
    notional: Money
    slippage_percent: Money
    created_at: UtcTimestamp


# ---------------------------------------------------------------------------
# WebSocket events
# ---------------------------------------------------------------------------
class TickEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "tick",
                    "ticker": "BTCUSDT",
                    "bid": "64996.75000000",
                    "ask": "65003.25000000",
                    "last": "65000.00000000",
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["tick"] = "tick"
    ticker: Ticker
    bid: Money
    ask: Money
    last: Money
    timestamp: UtcTimestamp


class OrderStatusEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "order_status",
                    "user_id": 1,
                    "order_id": 42,
                    "ticker": "BTCUSDT",
                    "side": "BUY",
                    "status": "FILLED",
                    "price": "65000.00000000",
                    "reason": None,
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["order_status"] = "order_status"
    user_id: int
    order_id: int
    ticker: str
    side: str
    status: OrderStatus
    price: Money | None = None
    filled_qty: Money = Decimal("0")
    fee_amount: Money = Decimal("0")
    price_improvement: Money = Decimal("0")
    reason: str | None = None
    timestamp: UtcTimestamp


class PositionClosedEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "position_closed",
                    "user_id": 1,
                    "order_id": 42,
                    "ticker": "BTCUSDT",
                    "quantity": "0.10000000",
                    "entry_price": "65000.00000000",
                    "exit_price": "66000.00000000",
                    "pnl": "100.00000000",
                    "reason": "TAKE_PROFIT",
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["position_closed"] = "position_closed"
    user_id: int
    order_id: int | None
    ticker: str
    quantity: Money
    entry_price: Money
    exit_price: Money
    pnl: Money
    reason: Literal["MANUAL", "STOP_LOSS", "TAKE_PROFIT"]
    timestamp: UtcTimestamp


class CopyTradeResultEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "copy_trade_result",
                    "user_id": 2,
                    "master_id": 1,
                    "master_order_id": 42,
                    "slave_order_id": 43,
                    "ticker": "BTCUSDT",
                    "status": "SKIPPED",
                    "reason": "SLIPPAGE_EXCEEDED",
                    "details": {
                        "slippage_percent": "1.50000000",
                        "max_slippage_percent": "1.00000000",
                    },
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["copy_trade_result"] = "copy_trade_result"
    user_id: int
    master_id: int
    master_order_id: int
    slave_order_id: int | None
    ticker: str
    status: Literal["EXECUTED", "SKIPPED"]
    reason: Literal["SLIPPAGE_EXCEEDED", "INSUFFICIENT_FUNDS"] | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: UtcTimestamp


class BalanceUpdatedEvent(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "balance_updated",
                    "user_id": 1,
                    "cash_balance": "9000.00000000",
                    "frozen_balance": "1000.00000000",
                    "reason": "ORDER_RESERVED",
                    "timestamp": "2026-11-19T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["balance_updated"] = "balance_updated"
    user_id: int
    cash_balance: Money
    frozen_balance: Money
    reason: Literal[
        "DEPOSIT",
        "ORDER_RESERVED",
        "ORDER_RELEASED",
        "ORDER_FILLED",
        "POSITION_CLOSED",
        "COPY_TRADE",
    ]
    timestamp: UtcTimestamp
