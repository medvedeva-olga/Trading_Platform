from http import HTTPStatus

from src.clients.validators import expect_model
from src.constants import OrderSide, TimeInForce, Ticker
from src.fixtures import order_payload
from src.schemas.order import OrderResponse
from src.schemas.portfolio import PortfolioSummary
from tests.config import settings as qa

UNREACHABLE = "0.00000010"

async def test_cancel_pending_gtc_order_releases_frozen_balance(master):

    created = expect_model(
        await master.client.create_order(
            order_payload(
                ticker= Ticker.ETHUSDT,
                side=OrderSide.BUY,
                amount=qa.eth_amount,
                min_price= UNREACHABLE,
                max_price=UNREACHABLE,
                time_in_force=TimeInForce.GTC,
            )
        ),
        OrderResponse,
        expected=HTTPStatus.CREATED,
    )
    before = expect_model(
        await master.client.portfolio_summary(), PortfolioSummary, expected=HTTPStatus.OK
    )

    response = await master.client.cancel_order(created.id)

    canceled = expect_model(response, OrderResponse, expected=HTTPStatus.OK)
    assert canceled.status == "CANCELED"
    assert canceled.reason == "CANCELED_BY_USER"
    after = expect_model(
        await master.client.portfolio_summary(), PortfolioSummary, expected=HTTPStatus.OK
    )
    assert after.frozen_balance == 0
    assert after.cash_balance == before.cash_balance + created.reserved_amount

