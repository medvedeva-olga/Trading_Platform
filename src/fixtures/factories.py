from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any


def unique_username(prefix: str) -> str:
    return f"qa_{prefix}_{uuid.uuid4().hex[:12]}"


def register_payload(username: str, password: str, role: str) -> dict[str, Any]:
    return {"username": username, "password": password, "role": role}


def corridor(
    price: float, low_factor: float = 0.8, high_factor: float = 1.2
) -> tuple[str, str]:
    low = (Decimal(str(price)) * Decimal(str(low_factor))).quantize(
        Decimal("0.00000001")
    )
    high = (Decimal(str(price)) * Decimal(str(high_factor))).quantize(
        Decimal("0.00000001")
    )
    return format(low, "f"), format(high, "f")


def order_payload(
    *,
    ticker: str,
    side: str,
    amount: str,
    min_price: str,
    max_price: str,
    time_in_force: str = "IOC",
    stop_loss: str | None = None,
    take_profit: str | None = None,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "side": side,
        "amount": amount,
        "min_price": min_price,
        "max_price": max_price,
        "time_in_force": time_in_force,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
    }


def subscribe_payload(
    *,
    master_id: int,
    allocation_usdt: str,
    copy_mode: str,
    fixed_ratio: str | None = None,
    max_slippage_percent: str = "1.00000000",
) -> dict[str, Any]:
    return {
        "master_id": master_id,
        "allocation_usdt": allocation_usdt,
        "copy_mode": copy_mode,
        "fixed_ratio": fixed_ratio,
        "max_slippage_percent": max_slippage_percent,
    }
