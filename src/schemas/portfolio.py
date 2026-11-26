from pydantic import BaseModel, ConfigDict, Field

from .common import ORMModel
from .types import MoneyStr


class DepositRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"amount": "10000.00000000"}]}
    )

    amount: MoneyStr = Field(
        gt=0,
        examples=["10000.00000000"],
        description="Deposit size in USDT, between 100 and 1 000 000.",
    )


class DepositResponse(BaseModel):
    transaction_id: int
    cash_balance: MoneyStr
    frozen_balance: MoneyStr

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
    quantity: MoneyStr
    avg_entry_price: MoneyStr
    mark_price: MoneyStr = Field(description="Bid price used for valuation.")
    market_value: MoneyStr
    unrealized_pnl: MoneyStr
    stop_loss: MoneyStr | None
    take_profit: MoneyStr | None


class PortfolioSummary(BaseModel):
    user_id: int
    username: str
    role: str
    cash_balance: MoneyStr = Field(description="Free USDT available for new orders.")
    frozen_balance: MoneyStr = Field(description="USDT reserved by pending GTC orders.")
    total_balance: MoneyStr = Field(description="`cash_balance + frozen_balance`.")
    positions: list[PositionSchema]
    positions_market_value: MoneyStr
    unrealized_pnl: MoneyStr
    realized_pnl: MoneyStr
    equity: MoneyStr = Field(description="`total_balance + positions_market_value`.")

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


class ExposurePosition(BaseModel):
    ticker: str
    quantity: MoneyStr
    mark_price: MoneyStr
    exposure: MoneyStr


class AccountExposure(BaseModel):
    user_id: int
    total_exposure: MoneyStr
    positions: list[ExposurePosition]
