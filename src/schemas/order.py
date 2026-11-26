from decimal import Decimal

from src.constants import OrderSide

from .common import ORMModel
from .types import MoneyStr, UtcTimestampStr


class OrderResponse(ORMModel):
    id: int
    user_id: int
    ticker: str
    side: OrderSide
    order_type: str
    amount: MoneyStr
    filled_qty: MoneyStr = Decimal("0")
    remaining_qty: MoneyStr = Decimal("0")
    min_price: MoneyStr
    max_price: MoneyStr
    time_in_force: str
    stop_loss: MoneyStr | None
    take_profit: MoneyStr | None
    status: str
    filled_price: MoneyStr | None
    reserved_amount: MoneyStr
    fee_amount: MoneyStr = Decimal("0")
    price_improvement: MoneyStr = Decimal("0")
    reason: str | None
    is_copy: bool
    copy_of_order_id: int | None
    created_at: UtcTimestampStr
    updated_at: UtcTimestampStr
    filled_at: UtcTimestampStr | None
