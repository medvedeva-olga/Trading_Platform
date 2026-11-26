from enum import StrEnum


class Role(StrEnum):
    MASTER = "MASTER"
    SLAVE = "SLAVE"


class Ticker(StrEnum):
    BTCUSDT = "BTCUSDT"
    ETHUSDT = "ETHUSDT"
    SOLUSDT = "SOLUSDT"
    TONUSDT = "TONUSDT"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELED = "CANCELED"


class CopyTradingStatus(StrEnum):
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"


class CopyMode(StrEnum):
    FIXED_RATIO = "FIXED_RATIO"
    PROPORTIONAL = "PROPORTIONAL"


class StreamAction(StrEnum):
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    SET = "set"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(StrEnum):
    IOC = "IOC"
    GTC ="GTC"

class SkipReason(StrEnum):
    SLIPPAGE_EXCEEDED = "SLIPPAGE_EXCEEDED"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"


class WsEvent(StrEnum):
    CONNECTED = "connected"
    TICK = "tick"
    ORDER_STATUS = "order_status"
    SUBSCRIPTION = "subscription"
    ERROR = "error"
    BALANCE_UPDATED = "balance_updated"
    COPY_TRADE_RESULT = "copy_trade_result"
