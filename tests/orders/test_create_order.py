from decimal import Decimal
from http import HTTPStatus

import pytest

from src.clients.validators import (
    expect_custom_error,
    expect_model,
    expect_model_list,
    expect_status,
    expect_validation_error,
)
from src.constants import OrderSide, OrderStatus, OrderType, Ticker, TimeInForce
from src.fixtures import corridor, order_payload
from src.messages import ErrCodes, OrderError
from src.schemas.market_data import TickerQuote
from src.schemas.order import OrderResponse
from src.schemas.portfolio import PortfolioSummary
from src.schemas.types import MONEY_QUANTUM
from tests.config import settings as qa

UNREACHABLE = "0.00000010"


async def _btc_quote(client) -> TickerQuote:
    """Вернуть котировку BTCUSDT из списка тикеров."""
    quotes = expect_model_list(await client.tickers(), TickerQuote)
    quote = next((q for q in quotes if q.ticker == Ticker.BTCUSDT), None)
    assert quote is not None, f"{Ticker.BTCUSDT} ticker not found"
    return quote


@pytest.mark.smoke
async def test_create_ioc_order_inside_corridor_returns_filled(master):
    quote = await _btc_quote(master.client)
    low, high = corridor(quote.ask)

    response = await master.client.create_order(
        order_payload(
            ticker=Ticker.BTCUSDT,
            side=OrderSide.BUY,
            amount=qa.btc_amount,
            min_price=low,
            max_price=high,
            time_in_force=TimeInForce.IOC,
        )
    )

    order = expect_model(response, OrderResponse, expected=HTTPStatus.CREATED)
    assert order.status == OrderStatus.FILLED
    assert order.filled_at is not None
    assert order.filled_price is not None
    assert Decimal(low) <= order.filled_price <= Decimal(high)
    assert order.is_copy is False


async def test_create_ioc_order_outside_corridor_returns_canceled(master):
    payload = order_payload(
        ticker=Ticker.BTCUSDT,
        side=OrderSide.BUY,
        amount=qa.btc_amount,
        min_price=UNREACHABLE,
        max_price=UNREACHABLE,
        time_in_force=TimeInForce.IOC,
    )

    response = await master.client.create_order(payload)

    order = expect_model(response, OrderResponse, expected=HTTPStatus.CREATED)
    assert order.status == OrderStatus.CANCELED
    assert order.reason == "IOC_OUT_OF_RANGE"
    assert order.filled_price is None


async def test_create_gtc_order_outside_corridor_reserves_funds(master):
    payload = order_payload(
        ticker=Ticker.ETHUSDT,
        side=OrderSide.BUY,
        amount=qa.eth_amount,
        min_price=UNREACHABLE,
        max_price=UNREACHABLE,
        time_in_force=TimeInForce.GTC,
    )

    response = await master.client.create_order(payload)

    order = expect_model(response, OrderResponse, expected=HTTPStatus.CREATED)
    assert order.status == OrderStatus.NEW
    assert order.reason == "AWAITING_RANGE"
    # зарезервировано amount* max_price
    assert order.reserved_amount == Decimal(qa.eth_amount) * Decimal(
        UNREACHABLE
    ).quantize(MONEY_QUANTUM)
    summary = expect_model(
        await master.client.portfolio_summary(),
        PortfolioSummary,
        expected=HTTPStatus.OK,
    )
    assert summary.frozen_balance == order.reserved_amount


async def test_create_market_gtc_order_returns_400(master):
    quote = await _btc_quote(master.client)
    low, high = corridor(quote.ask)

    response = await master.client.create_order(
        order_payload(
            ticker=Ticker.BTCUSDT,
            side=OrderSide.BUY,
            amount=qa.btc_amount,
            min_price=low,
            max_price=high,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
        )
    )

    expect_custom_error(
        response, ErrCodes.MARKET_GTC_NOT_ALLOWED, status=HTTPStatus.BAD_REQUEST
    )


async def test_create_order_with_reversed_corridor_returns_422(master):
    payload = {
        "ticker": Ticker.BTCUSDT,
        "side": OrderSide.BUY,
        "amount": qa.btc_amount,
        "min_price": "70000.00000000",
        "max_price": "60000.00000000",
        "time_in_force": TimeInForce.IOC,
        "stop_loss": None,
        "take_profit": None,
    }

    response = await master.client.create_order(payload)

    expect_validation_error(
        response, OrderError.MIN_PRICE_GR_MAX, HTTPStatus.UNPROCESSABLE_ENTITY
    )


async def test_create_order_with_unknown_ticker_returns_422(master):
    payload = {
        "ticker": "FAKEUSDT",
        "side": OrderSide.BUY,
        "amount": qa.btc_amount,
        "min_price": "1.00000000",
        "max_price": "2.00000000",
        "time_in_force": TimeInForce.IOC,
        "stop_loss": None,
        "take_profit": None,
    }

    response = await master.client.create_order(payload)

    expect_status(response, HTTPStatus.UNPROCESSABLE_ENTITY)


async def test_create_buy_order_without_funds_returns_400(user_factory):
    user = await user_factory("MASTER", amount="100.00000000")
    quote = await _btc_quote(user.client)
    low, high = corridor(quote.ask)

    response = await user.client.create_order(
        order_payload(
            ticker=Ticker.BTCUSDT,
            side=OrderSide.BUY,
            amount="1.00000000",
            min_price=low,
            max_price=high,
        )
    )

    detail = expect_custom_error(
        response, "INSUFFICIENT_FUNDS", status=HTTPStatus.BAD_REQUEST
    )
    assert Decimal(detail["details"]["required"]) > Decimal(
        detail["details"]["available"]
    )


async def test_sell_more_than_open_position_returns_400(master):
    quote = await _btc_quote(master.client)
    low, high = corridor(quote.ask)
    await master.client.create_order(
        order_payload(
            ticker=Ticker.BTCUSDT,
            side=OrderSide.BUY,
            amount="0.10000000",
            min_price=low,
            max_price=high,
        )
    )

    response = await master.client.create_order(
        order_payload(
            ticker=Ticker.BTCUSDT,
            side="SELL",
            amount="1.00000000",
            min_price=low,
            max_price=high,
        )
    )

    expect_custom_error(
        response, "INSUFFICIENT_POSITION", status=HTTPStatus.BAD_REQUEST
    )
