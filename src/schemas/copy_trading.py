from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.constants import CopyMode, SkipReason

from .common import ORMModel
from .error import BusinessError
from .types import MoneyStr, UtcTimestampStr

MASTER_ID_IMMUTABLE_MESSAGE = (
    "master_id is immutable for an existing subscription. "
    "Use POST /api/v1/copy-trading/subscribe to create a new subscription "
    "for another master."
)


class CopySubscriptionUpdate(BaseModel):
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

    allocation_usdt: MoneyStr | None = Field(
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
    fixed_ratio: MoneyStr | None = Field(
        default=None,
        gt=0,
        le=1,
        examples=["0.10000000"],
        description="New fixed share of the Master order amount.",
    )
    max_slippage_percent: MoneyStr | None = Field(
        default=None,
        ge=0,
        le=100,
        examples=["1.00000000"],
        description="New maximum allowed price deviation, in percent.",
    )

    @model_validator(mode="after")
    def validate_mode(self) -> CopySubscriptionUpdate:
        if self.copy_mode == CopyMode.FIXED_RATIO and self.fixed_ratio is None:
            raise ValueError(
                f"fixed_ratio is required when copy_mode is {CopyMode.FIXED_RATIO}"
            )
        return self


class SubscriptionResponse(ORMModel):
    id: int
    slave_id: int
    master_id: int
    allocation_usdt: MoneyStr
    copy_mode: str
    fixed_ratio: MoneyStr | None
    max_slippage_percent: MoneyStr
    active: bool
    created_at: UtcTimestampStr
    updated_at: UtcTimestampStr


class MasterStats(BaseModel):
    user_id: int
    username: str
    equity: MoneyStr
    total_deposits: MoneyStr
    realized_pnl: MoneyStr
    unrealized_pnl: MoneyStr
    trades_count: int
    subscribers_count: int
    roi_percent: MoneyStr

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
    skip_reason: SkipReason | None
    ticker: str
    amount: MoneyStr
    master_price: MoneyStr
    copy_price: MoneyStr
    notional: MoneyStr
    slippage_percent: MoneyStr
    created_at: UtcTimestampStr
