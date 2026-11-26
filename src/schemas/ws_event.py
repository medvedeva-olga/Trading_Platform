from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.constants import (
    CopyTradingStatus,
    OrderStatus,
    SkipReason,
    Ticker,
    WsEvent,
)

from .types import MoneyStr, UtcTimestampStr


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

    event: Literal[WsEvent.TICK] = WsEvent.TICK
    ticker: Ticker
    bid: MoneyStr
    ask: MoneyStr
    last: MoneyStr
    timestamp: UtcTimestampStr


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

    event: Literal[WsEvent.ORDER_STATUS] = WsEvent.ORDER_STATUS
    user_id: int
    order_id: int
    ticker: Ticker
    side: str
    status: OrderStatus
    price: MoneyStr | None = None
    filled_qty: MoneyStr = Decimal("0")
    fee_amount: MoneyStr = Decimal("0")
    price_improvement: MoneyStr = Decimal("0")
    reason: str | None = None
    timestamp: UtcTimestampStr


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

    event: Literal[WsEvent.COPY_TRADE_RESULT] = WsEvent.COPY_TRADE_RESULT
    user_id: int
    master_id: int
    master_order_id: int
    slave_order_id: int | None
    ticker: Ticker
    status: CopyTradingStatus
    reason: (
        Literal[SkipReason.SLIPPAGE_EXCEEDED, SkipReason.INSUFFICIENT_FUNDS] | None
    ) = None
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: UtcTimestampStr


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

    event: Literal[WsEvent.BALANCE_UPDATED] = WsEvent.BALANCE_UPDATED
    user_id: int
    cash_balance: MoneyStr
    frozen_balance: MoneyStr
    reason: Literal[
        "DEPOSIT",
        "ORDER_RESERVED",
        "ORDER_RELEASED",
        "ORDER_FILLED",
        "POSITION_CLOSED",
        "COPY_TRADE",
    ]
    timestamp: UtcTimestampStr
