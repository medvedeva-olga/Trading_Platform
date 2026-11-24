from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

MONEY_QUANTUM = Decimal("0.00000001")


def serialize_money(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM), "f")


def serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


MoneyStr = Annotated[
    Decimal, PlainSerializer(serialize_money, return_type=str, when_used="json")
]
UtcTimestampStr = Annotated[
    datetime, PlainSerializer(serialize_datetime, return_type=str, when_used="json")
]
