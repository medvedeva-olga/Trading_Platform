import asyncio
import json
from http import HTTPStatus

import pytest
import websockets

from src.clients.trading_api import open_market_stream, open_user_stream, recv_json
from src.clients.validators import (
    expect_event,
    expect_model,
    expect_model_list,
    expect_status,
)
from src.constants import (
    CopyMode,
    CopyTradingStatus,
    OrderSide,
    OrderStatus,
    StreamAction,
    Ticker,
    WsEvent,
)
from src.fixtures import corridor, order_payload, subscribe_payload
from src.schemas.copy_trading import SubscriptionResponse
from src.schemas.market_data import TickerQuote
from src.schemas.order import OrderResponse
from src.schemas.ws_event import (
    BalanceUpdatedEvent,
    CopyTradeResultEvent,
    OrderStatusEvent,
    TickEvent,
)
from tests.config import settings as qa


async def _next_event(socket, event_name: str, *, timeout: float, **fields):
    try:
        async with asyncio.timeout(timeout):
            while True:
                message = await recv_json(socket)
                if message.get("event") != event_name:
                    continue
                if all(message.get(key) == value for key, value in fields.items()):
                    return message
    except TimeoutError:
        pytest.fail(f"Событие {event_name} не получено за {timeout} с")


@pytest.mark.smoke
async def test_market_stream_delivers_ticks_for_subscribed_ticker(runtime, server):
    async with open_market_stream(runtime.base_url, [Ticker.BTCUSDT]) as socket:
        event = await _next_event(
            socket, WsEvent.TICK, timeout=qa.poll_timeout, ticker=Ticker.BTCUSDT
        )

    tick = expect_event(event, TickEvent)
    assert tick.ticker == Ticker.BTCUSDT
    assert 0 < tick.bid <= tick.last <= tick.ask


async def test_market_stream_subscription_ack_returns_requested_tickers(
    runtime, server
):
    async with open_market_stream(runtime.base_url) as socket:
        await socket.send(
            json.dumps({"action": StreamAction.SUBSCRIBE, "tickers": [Ticker.ETHUSDT]})
        )
        ack = await _next_event(socket, WsEvent.SUBSCRIPTION, timeout=qa.poll_timeout)

    assert ack["action"] == StreamAction.SUBSCRIBE
    assert ack["tickers"] == [Ticker.ETHUSDT]


async def test_market_stream_reports_unknown_ticker(runtime, server):
    async with open_market_stream(runtime.base_url) as socket:
        await socket.send(
            json.dumps({"action": StreamAction.SUBSCRIBE, "tickers": ["UNKNOWNUSDT"]})
        )
        error = await _next_event(socket, WsEvent.ERROR, timeout=qa.poll_timeout)

    assert "UNKNOWNUSDT" in error["detail"]


async def test_user_stream_rejects_invalid_token_with_4401(runtime, server):

    with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
        async with open_user_stream(runtime.base_url, "invalid.jwt.token") as socket:
            await socket.recv()
    assert exc_info.value.code == 4401


async def test_user_stream_pushes_balance_updated_after_deposit(runtime, master):
    async with open_user_stream(runtime.base_url, master.token) as socket:
        assert (await recv_json(socket))["event"] == WsEvent.CONNECTED

        response = await master.client.deposit("100.00000000")

        expect_status(response, 200)
        event = await _next_event(
            socket, WsEvent.BALANCE_UPDATED, timeout=qa.poll_timeout
        )
        balance = expect_event(event, BalanceUpdatedEvent)
        assert balance.reason == "DEPOSIT"
        assert balance.user_id == master.id


async def test_user_stream_pushes_order_status_when_order_is_filled(runtime, master):
    quotes = expect_model_list(await master.client.tickers(), TickerQuote)
    btc = next(q for q in quotes if q.ticker == Ticker.BTCUSDT)
    low, high = corridor(btc.ask)

    async with open_user_stream(runtime.base_url, master.token) as socket:
        assert (await recv_json(socket))["event"] == WsEvent.CONNECTED

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
        event = await _next_event(
            socket,
            WsEvent.ORDER_STATUS,
            timeout=qa.poll_timeout,
            status=OrderStatus.FILLED,
        )
        status = expect_event(event, OrderStatusEvent)
        assert status.order_id == order.id
        assert status.ticker == Ticker.BTCUSDT


async def test_user_stream_pushes_copy_trade_result(runtime, master, slave):
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
    quotes = expect_model_list(await master.client.tickers(), TickerQuote)
    btc = next(q for q in quotes if q.ticker == Ticker.BTCUSDT)
    low, high = corridor(btc.ask)

    async with open_user_stream(runtime.base_url, slave.token) as socket:
        assert (await recv_json(socket))["event"] == WsEvent.CONNECTED

        expect_model(
            await master.client.create_order(
                order_payload(
                    ticker=Ticker.BTCUSDT,
                    side=OrderSide.BUY,
                    amount=qa.btc_amount,
                    min_price=low,
                    max_price=high,
                )
            ),
            OrderResponse,
            expected=HTTPStatus.CREATED,
        )

        event = await _next_event(
            socket, WsEvent.COPY_TRADE_RESULT, timeout=qa.poll_timeout
        )
        result = expect_event(event, CopyTradeResultEvent)
        assert result.user_id == slave.id
        assert result.status == CopyTradingStatus.EXECUTED
        assert result.slave_order_id is not None
