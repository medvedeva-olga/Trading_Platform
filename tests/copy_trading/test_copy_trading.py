from decimal import Decimal
from http import HTTPStatus

import pytest

from src.clients.validators import (
    expect_custom_error,
    expect_error,
    expect_model,
    expect_model_list,
    expect_status,
)
from src.constants import (
    CopyMode,
    CopyTradingStatus,
    OrderSide,
    OrderStatus,
    Role,
    SkipReason,
    Ticker,
)
from src.fixtures import corridor, order_payload, subscribe_payload, wait_until
from src.messages import CopyTradingErrors, ErrCodes
from src.schemas.copy_trading import CopyHistoryItem, MasterStats, SubscriptionResponse
from src.schemas.market_data import TickerQuote
from src.schemas.order import OrderResponse
from src.schemas.types import MONEY_QUANTUM
from tests.config import settings as qa


async def _master_buy(master) -> OrderResponse:
    quotes = expect_model_list(await master.client.tickers(), TickerQuote)
    btc = next((q for q in quotes if q.ticker == Ticker.BTCUSDT), None)
    assert btc is not None, f"{Ticker.BTCUSDT} ticker not found"
    low, high = corridor(btc.ask)
    response = await master.client.create_order(
        order_payload(
            ticker=Ticker.BTCUSDT,
            side=OrderSide.BUY,
            amount=qa.btc_amount,
            min_price=low,
            max_price=high,
        )
    )
    order = expect_model(response, OrderResponse, expected=HTTPStatus.CREATED)
    assert (
        order.status == OrderStatus.FILLED
    ), f"Expected {OrderStatus.FILLED}, got {order.status} (order_id={order.id})"
    return order


async def _wait_for_copy_trading_history(client) -> list[CopyHistoryItem]:
    async def ready() -> list[CopyHistoryItem] | None:
        items = expect_model_list(await client.copy_history(), CopyHistoryItem)
        return items or None

    return await wait_until(
        ready,
        timeout=qa.poll_timeout,
        interval=qa.poll_interval,
        description="copy trading history entry",
    )


async def test_masters_list_contains_registered_master_with_statistics(master, slave):
    response = await slave.client.masters()

    expect_status(response, HTTPStatus.OK)
    masters = expect_model_list(response, MasterStats)
    master_stats = next((m for m in masters if m.user_id == master.id), None)
    assert master_stats is not None
    assert master_stats.username == master.username
    assert master_stats.total_deposits == Decimal(qa.master_deposit)
    assert master_stats.subscribers_count == 0
    assert master_stats.trades_count == 0


@pytest.mark.smoke
async def test_slave_can_subscribe_to_master(master, slave):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )

    response = await slave.client.subscribe(payload)

    subscription = expect_model(
        response, SubscriptionResponse, expected=HTTPStatus.CREATED
    )
    assert subscription.slave_id == slave.id
    assert subscription.master_id == master.id
    assert subscription.active is True
    assert subscription.copy_mode == CopyMode.FIXED_RATIO
    assert subscription.fixed_ratio == Decimal("0.10000000")


async def test_duplicate_subscription_returns_409(master, slave):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )

    response = await slave.client.subscribe(payload)
    response = await slave.client.subscribe(payload)

    detail = expect_custom_error(
        response,
        ErrCodes.SUBSCRIPTION_ALREADY_EXISTS,
        CopyTradingErrors.SUBSCRIPTION_ALREADY_EXISTS,
        status=HTTPStatus.CONFLICT,
    )
    assert detail["details"]["master_id"] == master.id


async def test_update_subscription_changes_params_keeping_id(master, slave):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )
    original = expect_model(
        await slave.client.subscribe(payload),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )

    response = await slave.client.update_subscription(
        original.id,
        {"fixed_ratio": "0.25000000", "max_slippage_percent": "2.00000000"},
    )

    updated = expect_model(response, SubscriptionResponse, expected=HTTPStatus.OK)
    assert updated.id == original.id
    assert updated.master_id == original.master_id
    assert updated.created_at == original.created_at
    assert updated.fixed_ratio == Decimal("0.25000000")
    assert updated.max_slippage_percent == Decimal("2.00000000")
    assert updated.active is True


async def test_update_subscription_with_master_id_forbidden(master, slave):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )
    original = expect_model(
        await slave.client.subscribe(payload),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )
    response = await slave.client.update_subscription(
        original.id,
        {"master_id": 0},
    )
    expect_custom_error(
        response,
        ErrCodes.MASTER_ID_IMMUTABLE,
        CopyTradingErrors.MASTER_ID_IMMUTABLE,
        status=HTTPStatus.BAD_REQUEST,
    )


async def test_update_deactivated_subscription_returns_409(master, slave):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )
    subscription = expect_model(
        await slave.client.subscribe(payload),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )

    expect_status(
        await slave.client.unsubscribe(subscription.id), HTTPStatus.NO_CONTENT
    )

    response = await slave.client.update_subscription(
        subscription.id, {CopyMode.FIXED_RATIO.lower(): "0.30000000"}
    )

    expect_custom_error(
        response,
        ErrCodes.SUBSCRIPTION_NOT_ACTIVE,
        CopyTradingErrors.SUBSCRIPTION_NOT_ACTIVE,
        status=HTTPStatus.CONFLICT,
    )
    subscriptions = expect_model_list(
        await slave.client.subscriptions(), SubscriptionResponse
    )
    assert subscriptions[0].fixed_ratio == Decimal("0.10000000")


async def test_subscribe_after_unsubscribe_creates_new_subscription(master, slave):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )
    first = expect_model(
        await slave.client.subscribe(payload),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )

    expect_status(await slave.client.unsubscribe(first.id), HTTPStatus.NO_CONTENT)

    second = expect_model(
        await slave.client.subscribe(payload),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )

    assert second.id != first.id
    assert second.active is True

    subscriptions = expect_model_list(
        await slave.client.subscriptions(), SubscriptionResponse
    )
    old = next(item for item in subscriptions if item.id == first.id)
    assert old.active is False
    assert len([item for item in subscriptions if item.master_id == master.id]) == 2


async def test_master_attempting_to_subscribe_itself_returns_403(master):
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt=qa.default_allocation,
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )

    response = await master.client.subscribe(payload)

    expect_error(response, CopyTradingErrors.SLAVE_ROLE_REQUIRED, HTTPStatus.FORBIDDEN)


async def test_subscribe_with_allocation_above_balance_returns_400(
    master, user_factory
):
    poor = await user_factory(Role.SLAVE, amount="100.00000000")
    payload = subscribe_payload(
        master_id=master.id,
        allocation_usdt="1000.00000000",
        copy_mode=CopyMode.FIXED_RATIO,
        fixed_ratio="0.10000000",
    )

    response = await poor.client.subscribe(payload)

    expect_custom_error(
        response,
        ErrCodes.INSUFFICIENT_FUNDS,
        CopyTradingErrors.INSUFFICIENT_FUNDS,
        status=HTTPStatus.BAD_REQUEST,
    )


async def test_master_fill_creates_executed_copy_for_slave(master, slave):
    expect_model(
        await slave.client.subscribe(
            subscribe_payload(
                master_id=master.id,
                allocation_usdt=qa.default_allocation,
                copy_mode=CopyMode.FIXED_RATIO,
                fixed_ratio="0.10000000",
                max_slippage_percent=qa.default_slippage,
            )
        ),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )
    master_order = await _master_buy(master)

    history = await _wait_for_copy_trading_history(slave.client)

    entry = history[0]
    assert entry.status == CopyTradingStatus.EXECUTED
    assert entry.skip_reason is None
    assert entry.slave_order_id is not None
    assert entry.master_order_id == master_order.id
    assert entry.amount == Decimal(qa.btc_amount) * Decimal("0.10000000")


async def test_copy_with_tight_slippage_is_skipped(master, slave):
    expect_model(
        await slave.client.subscribe(
            subscribe_payload(
                master_id=master.id,
                allocation_usdt=qa.default_allocation,
                copy_mode=CopyMode.FIXED_RATIO,
                fixed_ratio="0.10000000",
                max_slippage_percent=format(MONEY_QUANTUM, "f"),
            )
        ),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )
    await _master_buy(master)

    history = await _wait_for_copy_trading_history(slave.client)

    assert history[0].status == CopyTradingStatus.SKIPPED
    assert history[0].skip_reason == SkipReason.SLIPPAGE_EXCEEDED
    assert history[0].slave_order_id is None
    assert history[0].slippage_percent > MONEY_QUANTUM


async def test_copy_with_allocation_smaller_than_notional_is_skipped(
    master, user_factory
):
    poor = await user_factory(Role.SLAVE, amount="100.00000000")
    expect_model(
        await poor.client.subscribe(
            subscribe_payload(
                master_id=master.id,
                allocation_usdt="100.00000000",
                copy_mode=CopyMode.FIXED_RATIO,
                fixed_ratio="1.00000000",
                max_slippage_percent=qa.default_slippage,
            )
        ),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )
    await _master_buy(master)

    history = await _wait_for_copy_trading_history(poor.client)

    assert history[0].status == CopyTradingStatus.SKIPPED
    assert history[0].skip_reason == SkipReason.INSUFFICIENT_FUNDS
    assert history[0].notional > Decimal("100.00000000")


async def test_proportional_copy_uses_allocation_to_master_equity_ratio(master, slave):
    expect_model(
        await slave.client.subscribe(
            subscribe_payload(
                master_id=master.id,
                allocation_usdt=qa.default_allocation,
                copy_mode=CopyMode.PROPORTIONAL,
                max_slippage_percent=qa.default_slippage,
            )
        ),
        SubscriptionResponse,
        expected=HTTPStatus.CREATED,
    )
    await _master_buy(master)

    history = await _wait_for_copy_trading_history(slave.client)

    entry = history[0]
    assert entry.status == CopyTradingStatus.EXECUTED
    assert Decimal("0") < entry.amount <= Decimal(qa.btc_amount)
    assert entry.notional <= Decimal(qa.default_allocation)
