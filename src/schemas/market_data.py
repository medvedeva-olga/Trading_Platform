from pydantic import BaseModel, ConfigDict

from src.constants import Ticker

from .types import MoneyStr, UtcTimestampStr


class TickerQuote(BaseModel):
    ticker: Ticker
    bid: MoneyStr
    ask: MoneyStr
    last: MoneyStr
    spread: MoneyStr
    timestamp: UtcTimestampStr

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
    bids: list[tuple[MoneyStr, MoneyStr]]
    asks: list[tuple[MoneyStr, MoneyStr]]
    timestamp: UtcTimestampStr
