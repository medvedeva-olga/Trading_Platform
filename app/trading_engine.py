"""Business logic: accounts, orders, execution, risk levels and copy trading."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import TICKERS, ZERO, money, settings
from .database import SessionLocal
from .market_feed import MarketFeed, Quote
from .models import (
    CopySubscription,
    CopyTradeHistory,
    Order,
    Portfolio,
    Position,
    Trade,
    Transaction,
    User,
)
from .schemas import (
    BalanceUpdatedEvent,
    BusinessError,
    CopyHistoryItem,
    CopySubscribeRequest,
    CopySubscriptionUpdate,
    CopyTradeResultEvent,
    DepositResponse,
    MasterStats,
    OrderCreateRequest,
    OrderResponse,
    OrderStatusEvent,
    PortfolioSummary,
    PositionClosedEvent,
    PositionSchema,
    SubscriptionResponse,
    UserResponse,
)

if TYPE_CHECKING:
    from .ws_manager import ConnectionManager

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("NEW", "PARTIALLY_FILLED")
UNBOUNDED = Decimal("1E30")


@dataclass(frozen=True)
class BookLevel:
    """One executable price level (``order_id`` is None for synthetic liquidity)."""

    price: Decimal
    quantity: Decimal
    order_id: int | None = None


def now() -> datetime:
    return datetime.now(timezone.utc)


class TradingEngine:
    """Single writer lock guarantees consistent ledger mutations under load."""

    def __init__(self, feed: MarketFeed, ws: "ConnectionManager") -> None:
        self.feed = feed
        self.ws = ws
        self._lock = asyncio.Lock()
        self._copy_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------
    @staticmethod
    async def _portfolio(session: AsyncSession, user_id: int) -> Portfolio:
        portfolio = await session.get(Portfolio, user_id)
        if portfolio is None:
            raise BusinessError("PORTFOLIO_NOT_FOUND", "Portfolio does not exist.", 404)
        return portfolio

    @staticmethod
    async def _position(session: AsyncSession, user_id: int, ticker: str) -> Position:
        position = await session.scalar(
            select(Position).where(
                Position.user_id == user_id, Position.ticker == ticker
            )
        )
        if position is None:
            position = Position(
                user_id=user_id, ticker=ticker, quantity=ZERO, avg_entry_price=ZERO
            )
            session.add(position)
            await session.flush()
        return position

    @staticmethod
    def _balance_event(
        portfolio: Portfolio, user_id: int, reason: str
    ) -> BalanceUpdatedEvent:
        return BalanceUpdatedEvent(
            user_id=user_id,
            cash_balance=money(portfolio.cash_balance),
            frozen_balance=money(portfolio.frozen_balance),
            reason=reason,
            timestamp=now(),
        )

    @staticmethod
    def _order_event(
        order: Order,
        status: str,
        price: Decimal | None = None,
        reason: str | None = None,
    ) -> OrderStatusEvent:
        return OrderStatusEvent(
            user_id=order.user_id,
            order_id=order.id,
            ticker=order.ticker,
            side=order.side,
            status=status,
            price=money(price) if price is not None else None,
            filled_qty=money(order.filled_qty),
            fee_amount=money(order.fee_amount),
            price_improvement=money(order.price_improvement),
            reason=reason,
            timestamp=now(),
        )

    def _publish_to_user(self, user_id: int, event: Any) -> None:
        self.ws.publish_user(user_id, event.model_dump(mode="json"))

    # ------------------------------------------------------------------
    # Auth / users
    # ------------------------------------------------------------------
    @staticmethod
    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")

    @staticmethod
    def verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"), password_hash.encode("ascii")
            )
        except (ValueError, TypeError):
            return False

    async def register(self, username: str, password: str, role: str) -> UserResponse:
        async with self._lock, SessionLocal() as session:
            existing = await session.scalar(
                select(User).where(User.username == username)
            )
            if existing is not None:
                raise BusinessError(
                    "USERNAME_ALREADY_EXISTS",
                    "That username is already registered; use a unique fixture name.",
                )
            user = User(
                username=username, password_hash=self.hash_password(password), role=role
            )
            session.add(user)
            await session.flush()
            session.add(
                Portfolio(
                    user_id=user.id,
                    cash_balance=ZERO,
                    frozen_balance=ZERO,
                    realized_pnl=ZERO,
                )
            )
            await session.commit()
            return UserResponse.model_validate(user)

    async def authenticate(self, username: str, password: str) -> User | None:
        async with SessionLocal() as session:
            user = await session.scalar(select(User).where(User.username == username))
            if user is None or not self.verify_password(password, user.password_hash):
                return None
            # Detach the lightweight identity from the session before returning.
            return User(
                id=user.id, username=user.username, role=user.role, password_hash=""
            )

    # ------------------------------------------------------------------
    # Portfolio
    # ------------------------------------------------------------------
    async def deposit(self, user_id: int, amount: Decimal) -> DepositResponse:
        value = money(amount)
        if value < settings.min_deposit or value > settings.max_deposit:
            raise BusinessError(
                "INVALID_DEPOSIT_AMOUNT",
                f"Deposit must be between {settings.min_deposit} and {settings.max_deposit} USDT.",
                details={
                    "min": str(settings.min_deposit),
                    "max": str(settings.max_deposit),
                },
            )
        async with self._lock, SessionLocal() as session:
            portfolio = await self._portfolio(session, user_id)
            portfolio.cash_balance = money(portfolio.cash_balance + value)
            transaction = Transaction(
                user_id=user_id,
                type="DEPOSIT",
                amount=value,
                balance_after=portfolio.cash_balance,
                note="Virtual USDT deposit",
            )
            session.add(transaction)
            await session.flush()
            await session.commit()
            self._publish_to_user(
                user_id, self._balance_event(portfolio, user_id, "DEPOSIT")
            )
            return DepositResponse(
                transaction_id=transaction.id,
                cash_balance=money(portfolio.cash_balance),
                frozen_balance=money(portfolio.frozen_balance),
            )

    async def get_summary(self, user_id: int) -> PortfolioSummary:
        async with self._lock, SessionLocal() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise BusinessError("USER_NOT_FOUND", "User does not exist.", 404)
            portfolio = await self._portfolio(session, user_id)
            positions = await session.scalars(
                select(Position).where(
                    Position.user_id == user_id, Position.quantity > ZERO
                )
            )
            items: list[PositionSchema] = []
            total_market_value = ZERO
            total_unrealized = ZERO
            for position in positions:
                quote = self.feed.get_quote(position.ticker)
                market_value = money(position.quantity * quote.bid)
                unrealized = money(
                    position.quantity * (quote.bid - position.avg_entry_price)
                )
                total_market_value = money(total_market_value + market_value)
                total_unrealized = money(total_unrealized + unrealized)
                items.append(
                    PositionSchema(
                        ticker=position.ticker,
                        quantity=money(position.quantity),
                        avg_entry_price=money(position.avg_entry_price),
                        mark_price=money(quote.bid),
                        market_value=market_value,
                        unrealized_pnl=unrealized,
                        stop_loss=(
                            money(position.stop_loss)
                            if position.stop_loss is not None
                            else None
                        ),
                        take_profit=(
                            money(position.take_profit)
                            if position.take_profit is not None
                            else None
                        ),
                    )
                )
            total_balance = money(portfolio.cash_balance + portfolio.frozen_balance)
            return PortfolioSummary(
                user_id=user.id,
                username=user.username,
                role=user.role,
                cash_balance=money(portfolio.cash_balance),
                frozen_balance=money(portfolio.frozen_balance),
                total_balance=total_balance,
                positions=items,
                positions_market_value=total_market_value,
                unrealized_pnl=total_unrealized,
                realized_pnl=money(portfolio.realized_pnl),
                equity=money(total_balance + total_market_value),
            )

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    @staticmethod
    def _fee(price: Decimal, quantity: Decimal) -> Decimal:
        return money(price * quantity * settings.fee_rate)

    async def _book_levels(
        self, session: AsyncSession, ticker: str, side: str
    ) -> list[BookLevel]:
        """Executable levels for a taker of ``side``, best price first."""
        bids, asks = self.feed.get_depth(ticker)
        source = asks if side == "BUY" else bids
        maker_side = "SELL" if side == "BUY" else "BUY"
        levels = [
            BookLevel(money(price), money(quantity))
            for price, quantity in source
            if money(quantity) > ZERO
        ]
        rows = await session.scalars(
            select(Order)
            .where(
                Order.ticker == ticker,
                Order.side == maker_side,
                Order.status.in_(ACTIVE_STATUSES),
            )
            .order_by(
                (
                    Order.min_price.asc()
                    if maker_side == "SELL"
                    else Order.max_price.desc()
                ),
                Order.id.asc(),
            )
        )
        for maker in rows:
            remaining = money(maker.remaining_qty)
            if remaining <= ZERO:
                continue
            price = (
                money(maker.min_price)
                if maker_side == "SELL"
                else money(maker.max_price)
            )
            levels.append(BookLevel(price, remaining, maker.id))

        reverse = side == "SELL"
        levels.sort(
            key=lambda level: (
                -level.price if reverse else level.price,
                level.order_id or 0,
            )
        )
        return levels

    async def _apply_fill(
        self,
        session: AsyncSession,
        order: Order,
        price: Decimal,
        quantity: Decimal,
        *,
        limit_price: Decimal | None = None,
    ) -> list[Any]:
        """Execute part of an order, update the ledger and return WS events."""
        price = money(price)
        quantity = money(quantity)
        remaining_before = money(order.remaining_qty)
        if quantity <= ZERO or remaining_before <= ZERO:
            return []

        portfolio = await self._portfolio(session, order.user_id)
        reserved = money(order.reserved_amount)
        release = money(reserved * quantity / remaining_before)
        if release > ZERO:
            portfolio.frozen_balance = money(portfolio.frozen_balance - release)
            portfolio.cash_balance = money(portfolio.cash_balance + release)
            order.reserved_amount = money(reserved - release)

        fee = self._fee(price, quantity)
        improvement = ZERO
        events: list[Any] = []
        if order.side == "BUY":
            cost = money(price * quantity)
            if portfolio.cash_balance < money(cost + fee):
                raise BusinessError(
                    "INSUFFICIENT_FUNDS",
                    "Not enough free balance to fill the buy order.",
                    details={
                        "required": str(money(cost + fee)),
                        "available": str(money(portfolio.cash_balance)),
                    },
                )
            portfolio.cash_balance = money(portfolio.cash_balance - cost - fee)
            position = await self._position(session, order.user_id, order.ticker)
            new_quantity = money(position.quantity + quantity)
            position.avg_entry_price = (
                money(
                    (position.quantity * position.avg_entry_price + cost) / new_quantity
                )
                if new_quantity > ZERO
                else ZERO
            )
            position.quantity = new_quantity
            if order.stop_loss is not None:
                position.stop_loss = money(order.stop_loss)
            if order.take_profit is not None:
                position.take_profit = money(order.take_profit)
            position.source_order_id = order.id
            tx_amount = -(cost + fee)
            tx_type = "ORDER_BUY"
            note = f"BUY {quantity} {order.ticker} @ {price} (fee {fee})"
            if limit_price is not None:
                improvement = money(max(ZERO, limit_price - price) * quantity)
        else:
            position = await self._position(session, order.user_id, order.ticker)
            if position.quantity < quantity:
                raise BusinessError(
                    "INSUFFICIENT_POSITION",
                    "Long position was reduced before the order could be filled.",
                )
            proceeds = money(price * quantity)
            realized = money(quantity * (price - position.avg_entry_price))
            entry_price = money(position.avg_entry_price)
            portfolio.cash_balance = money(portfolio.cash_balance + proceeds - fee)
            portfolio.realized_pnl = money(portfolio.realized_pnl + realized)
            position.quantity = money(position.quantity - quantity)
            closed = position.quantity == ZERO
            if closed:
                position.avg_entry_price = ZERO
                position.stop_loss = None
                position.take_profit = None
                position.source_order_id = None
            tx_amount = proceeds - fee
            tx_type = "ORDER_SELL"
            note = f"SELL {quantity} {order.ticker} @ {price} (fee {fee})"
            if limit_price is not None:
                improvement = money(max(ZERO, price - limit_price) * quantity)
            if closed:
                events.append(
                    PositionClosedEvent(
                        user_id=order.user_id,
                        order_id=order.id,
                        ticker=order.ticker,
                        quantity=quantity,
                        entry_price=entry_price,
                        exit_price=price,
                        pnl=realized,
                        reason="MANUAL",
                        timestamp=now(),
                    )
                )

        session.add(
            Transaction(
                user_id=order.user_id,
                type=tx_type,
                amount=money(tx_amount),
                balance_after=money(portfolio.cash_balance),
                note=note,
            )
        )
        filled_before = money(order.filled_qty)
        filled_after = money(filled_before + quantity)
        weighted = (
            money(
                ((order.filled_price or ZERO) * filled_before + price * quantity)
                / filled_after
            )
            if filled_after > ZERO
            else price
        )
        order.filled_qty = filled_after
        order.remaining_qty = money(remaining_before - quantity)
        order.filled_price = weighted
        order.fee_amount = money(order.fee_amount + fee)
        order.price_improvement = money(order.price_improvement + improvement)
        order.updated_at = now()
        if order.remaining_qty == ZERO:
            order.status = "FILLED"
            order.filled_at = now()
            order.reason = None
        else:
            order.status = "PARTIALLY_FILLED"
        events.append(self._order_event(order, order.status, price, order.reason))
        events.append(self._balance_event(portfolio, order.user_id, "ORDER_FILLED"))
        return events

    async def create_order(
        self, user_id: int, data: OrderCreateRequest
    ) -> OrderResponse:
        if data.ticker not in TICKERS:
            raise BusinessError("INVALID_TICKER", f"Unsupported ticker: {data.ticker}")
        amount = money(data.amount)
        if amount < settings.min_order_amount:
            raise BusinessError(
                "AMOUNT_TOO_SMALL",
                f"Order amount must be at least {settings.min_order_amount}.",
            )

        async with self._lock, SessionLocal() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise BusinessError("USER_NOT_FOUND", "User does not exist.", 404)
            portfolio = await self._portfolio(session, user_id)
            quote = self.feed.get_quote(data.ticker)
            order_type = data.order_type or "LIMIT"
            is_market = order_type == "MARKET"
            floor = ZERO if is_market else money(data.min_price)
            cap = UNBOUNDED if is_market else money(data.max_price)
            reserve_price = (
                quote.ask
                if data.side == "BUY" and (is_market or data.time_in_force == "IOC")
                else (
                    cap
                    if data.side == "BUY"
                    else (
                        quote.bid
                        if is_market or data.time_in_force == "IOC"
                        else money(data.min_price)
                    )
                )
            )

            if data.side == "BUY":
                required = money(
                    amount * reserve_price * (Decimal(1) + settings.fee_rate)
                )
                if portfolio.cash_balance < required:
                    raise BusinessError(
                        "INSUFFICIENT_FUNDS",
                        "Not enough free balance for this order.",
                        details={
                            "required": str(required),
                            "available": str(money(portfolio.cash_balance)),
                        },
                    )
            else:
                position = await session.scalar(
                    select(Position).where(
                        Position.user_id == user_id, Position.ticker == data.ticker
                    )
                )
                if position is None or position.quantity < amount:
                    raise BusinessError(
                        "INSUFFICIENT_POSITION",
                        "No open long position large enough to sell.",
                        details={
                            "requested": str(amount),
                            "available": (
                                str(money(position.quantity))
                                if position
                                else "0.00000000"
                            ),
                        },
                    )
                # USDT-equivalent reservation, used only for the frozen_balance display.
                required = money(amount * reserve_price)
                if portfolio.cash_balance < required:
                    raise BusinessError(
                        "INSUFFICIENT_FUNDS",
                        "Not enough free balance to reserve the sell order.",
                        details={
                            "required": str(required),
                            "available": str(money(portfolio.cash_balance)),
                        },
                    )

            order = Order(
                user_id=user_id,
                ticker=data.ticker,
                side=data.side,
                order_type=order_type,
                amount=amount,
                filled_qty=ZERO,
                remaining_qty=amount,
                min_price=money(data.min_price),
                max_price=money(data.max_price),
                time_in_force=data.time_in_force,
                stop_loss=money(data.stop_loss) if data.stop_loss is not None else None,
                take_profit=(
                    money(data.take_profit) if data.take_profit is not None else None
                ),
                status="NEW",
                reserved_amount=ZERO,
            )
            session.add(order)
            await session.flush()

            events: list[Any] = []
            taker_limit = None if is_market else (cap if data.side == "BUY" else floor)
            for level in await self._book_levels(session, data.ticker, data.side):
                if order.remaining_qty <= ZERO:
                    break
                if level.price < floor or level.price > cap:
                    continue
                fill_qty = min(order.remaining_qty, level.quantity)
                fee_before = money(order.fee_amount)
                improvement_before = money(order.price_improvement)
                events.extend(
                    await self._apply_fill(
                        session,
                        order,
                        level.price,
                        fill_qty,
                        limit_price=taker_limit,
                    )
                )
                if level.order_id is not None and level.order_id != order.id:
                    maker = await session.get(Order, level.order_id)
                    if maker is not None:
                        maker_limit = (
                            money(maker.min_price)
                            if maker.side == "SELL"
                            else money(maker.max_price)
                        )
                        events.extend(
                            await self._apply_fill(
                                session,
                                maker,
                                level.price,
                                fill_qty,
                                limit_price=maker_limit,
                            )
                        )
                session.add(
                    Trade(
                        ticker=order.ticker,
                        side=order.side,
                        maker_order_id=level.order_id,
                        taker_order_id=order.id,
                        price=money(level.price),
                        quantity=money(fill_qty),
                        fee=money(order.fee_amount - fee_before),
                        price_improvement=money(
                            order.price_improvement - improvement_before
                        ),
                    )
                )

            if order.remaining_qty > ZERO:
                if data.time_in_force == "IOC":
                    order.status = "CANCELED"
                    order.reason = (
                        "IOC_PARTIALLY_FILLED"
                        if order.filled_qty > ZERO
                        else "IOC_OUT_OF_RANGE"
                    )
                    order.updated_at = now()
                    events.append(
                        self._order_event(order, "CANCELED", None, order.reason)
                    )
                else:
                    rest_price = cap if data.side == "BUY" else floor
                    reserve = (
                        money(
                            order.remaining_qty
                            * rest_price
                            * (Decimal(1) + settings.fee_rate)
                        )
                        if data.side == "BUY"
                        else money(order.remaining_qty * rest_price)
                    )
                    portfolio.cash_balance = money(portfolio.cash_balance - reserve)
                    portfolio.frozen_balance = money(portfolio.frozen_balance + reserve)
                    order.reserved_amount = reserve
                    order.status = (
                        "PARTIALLY_FILLED" if order.filled_qty > ZERO else "NEW"
                    )
                    order.reason = "AWAITING_RANGE"
                    events.append(
                        self._balance_event(portfolio, user_id, "ORDER_RESERVED")
                    )
                    events.append(
                        self._order_event(order, order.status, None, "AWAITING_RANGE")
                    )

            await session.commit()
            for event in events:
                self._publish_to_user(user_id, event)

            snapshot: dict[str, Any] | None = None
            if order.status == "FILLED" and user.role == "MASTER" and not order.is_copy:
                snapshot = {
                    "master_order_id": order.id,
                    "master_id": user_id,
                    "ticker": order.ticker,
                    "side": order.side,
                    "amount": money(order.amount),
                    "price": money(order.filled_price),
                    "master_equity": await self._master_equity(user_id),
                }
            if snapshot is not None:
                self._schedule_copy(snapshot)
            return OrderResponse.model_validate(order)

    async def list_orders(self, user_id: int, limit: int = 50) -> list[OrderResponse]:
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(Order)
                .where(Order.user_id == user_id)
                .order_by(Order.id.desc())
                .limit(limit)
            )
            return [OrderResponse.model_validate(row) for row in rows]

    async def get_order(self, user_id: int, order_id: int) -> Order:
        async with SessionLocal() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise BusinessError("ORDER_NOT_FOUND", "Order does not exist.", 404)
            if order.user_id != user_id:
                raise BusinessError("FORBIDDEN", "Order belongs to another user.", 403)
            return order

    async def cancel_order(self, user_id: int, order_id: int) -> OrderResponse:
        async with self._lock, SessionLocal() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise BusinessError("ORDER_NOT_FOUND", "Order does not exist.", 404)
            if order.user_id != user_id:
                raise BusinessError("FORBIDDEN", "Order belongs to another user.", 403)
            if order.status not in ACTIVE_STATUSES:
                raise BusinessError(
                    "ORDER_NOT_CANCELABLE", f"Order is already {order.status}."
                )

            portfolio = await self._portfolio(session, user_id)
            reserved = money(order.reserved_amount)
            portfolio.frozen_balance = money(portfolio.frozen_balance - reserved)
            portfolio.cash_balance = money(portfolio.cash_balance + reserved)
            order.status = "CANCELED"
            order.reason = "CANCELED_BY_USER"
            order.updated_at = now()
            session.add(
                Transaction(
                    user_id=user_id,
                    type="ORDER_CANCEL",
                    amount=reserved,
                    balance_after=portfolio.cash_balance,
                    note=f"Released reservation for order {order.id}",
                )
            )
            await session.commit()
            self._publish_to_user(
                user_id, self._order_event(order, "CANCELED", None, "CANCELED_BY_USER")
            )
            self._publish_to_user(
                user_id, self._balance_event(portfolio, user_id, "ORDER_RELEASED")
            )
            return OrderResponse.model_validate(order)

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------
    async def _fill_order_locked(
        self, session: AsyncSession, order: Order, price: Decimal
    ) -> list[Any]:
        """Fill the whole remaining quantity (used by copy orders)."""
        limit = (
            money(order.max_price) if order.side == "BUY" else money(order.min_price)
        )
        return await self._apply_fill(
            session,
            order,
            price,
            money(order.remaining_qty),
            limit_price=limit,
        )

    async def _close_position_locked(
        self,
        session: AsyncSession,
        position: Position,
        exit_price: Decimal,
        reason: str,
    ) -> list[Any]:
        portfolio = await self._portfolio(session, position.user_id)
        quantity = money(position.quantity)
        entry_price = money(position.avg_entry_price)
        exit_price = money(exit_price)
        proceeds = money(quantity * exit_price)
        pnl = money(quantity * (exit_price - entry_price))
        order_id = position.source_order_id

        portfolio.cash_balance = money(portfolio.cash_balance + proceeds)
        portfolio.realized_pnl = money(portfolio.realized_pnl + pnl)
        position.quantity = ZERO
        position.avg_entry_price = ZERO
        position.stop_loss = None
        position.take_profit = None
        position.source_order_id = None
        session.add(
            Transaction(
                user_id=position.user_id,
                type="POSITION_CLOSED",
                amount=proceeds,
                balance_after=money(portfolio.cash_balance),
                note=f"{reason} {position.ticker} @ {exit_price}",
            )
        )
        return [
            PositionClosedEvent(
                user_id=position.user_id,
                order_id=order_id,
                ticker=position.ticker,
                quantity=quantity,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl=pnl,
                reason=reason,
                timestamp=now(),
            ),
            self._balance_event(portfolio, position.user_id, "POSITION_CLOSED"),
        ]

    async def handle_tick(self, ticker: str, quote: Quote) -> None:
        """Match resting orders against the book and evaluate SL/TP levels."""
        async with self._lock, SessionLocal() as session:
            events: list[Any] = []
            master_fills: list[dict[str, Any]] = []

            resting = await session.scalars(
                select(Order).where(
                    Order.status.in_(ACTIVE_STATUSES), Order.ticker == ticker
                )
            )
            for order in resting:
                is_market = order.order_type == "MARKET"
                floor = ZERO if is_market else money(order.min_price)
                cap = UNBOUNDED if is_market else money(order.max_price)
                taker_limit = (
                    None if is_market else (cap if order.side == "BUY" else floor)
                )
                for level in await self._book_levels(session, ticker, order.side):
                    if order.remaining_qty <= ZERO:
                        break
                    if level.price < floor or level.price > cap:
                        continue
                    if level.order_id == order.id:
                        continue
                    fill_qty = min(order.remaining_qty, level.quantity)
                    fee_before = money(order.fee_amount)
                    improvement_before = money(order.price_improvement)
                    events.extend(
                        await self._apply_fill(
                            session,
                            order,
                            level.price,
                            fill_qty,
                            limit_price=taker_limit,
                        )
                    )
                    if level.order_id is not None:
                        maker = await session.get(Order, level.order_id)
                        if maker is not None:
                            maker_limit = (
                                money(maker.min_price)
                                if maker.side == "SELL"
                                else money(maker.max_price)
                            )
                            events.extend(
                                await self._apply_fill(
                                    session,
                                    maker,
                                    level.price,
                                    fill_qty,
                                    limit_price=maker_limit,
                                )
                            )
                    session.add(
                        Trade(
                            ticker=order.ticker,
                            side=order.side,
                            maker_order_id=level.order_id,
                            taker_order_id=order.id,
                            price=money(level.price),
                            quantity=money(fill_qty),
                            fee=money(order.fee_amount - fee_before),
                            price_improvement=money(
                                order.price_improvement - improvement_before
                            ),
                        )
                    )
                if order.status == "FILLED":
                    user = await session.get(User, order.user_id)
                    if user is not None and user.role == "MASTER" and not order.is_copy:
                        master_fills.append(
                            {
                                "master_order_id": order.id,
                                "master_id": order.user_id,
                                "ticker": order.ticker,
                                "side": order.side,
                                "amount": money(order.amount),
                                "price": money(order.filled_price),
                                "master_equity": await self._master_equity(
                                    order.user_id
                                ),
                            }
                        )

            positions = await session.scalars(
                select(Position).where(
                    Position.ticker == ticker, Position.quantity > ZERO
                )
            )
            for position in positions:
                trigger: str | None = None
                if position.stop_loss is not None and quote.last <= position.stop_loss:
                    trigger = "STOP_LOSS"
                elif (
                    position.take_profit is not None
                    and quote.last >= position.take_profit
                ):
                    trigger = "TAKE_PROFIT"
                if trigger is not None:
                    events.extend(
                        await self._close_position_locked(
                            session, position, quote.bid, trigger
                        )
                    )

            await session.commit()
            for event in events:
                self._publish_to_user(event.user_id, event)
            for snapshot in master_fills:
                self._schedule_copy(snapshot)

    async def _master_equity(self, user_id: int) -> Decimal:
        """Compute equity from a fresh short-lived session (used for copy ratios)."""
        async with SessionLocal() as session:
            portfolio = await self._portfolio(session, user_id)
            market_value = ZERO
            positions = await session.scalars(
                select(Position).where(
                    Position.user_id == user_id, Position.quantity > ZERO
                )
            )
            for position in positions:
                market_value = money(
                    market_value
                    + position.quantity * self.feed.get_quote(position.ticker).bid
                )
            return money(
                portfolio.cash_balance + portfolio.frozen_balance + market_value
            )

    # ------------------------------------------------------------------
    # Copy trading
    # ------------------------------------------------------------------
    async def get_masters(self) -> list[MasterStats]:
        async with SessionLocal() as session:
            masters = await session.scalars(
                select(User).where(User.role == "MASTER").order_by(User.id)
            )
            result: list[MasterStats] = []
            for master in masters:
                portfolio = await self._portfolio(session, master.id)
                market_value = ZERO
                unrealized = ZERO
                positions = await session.scalars(
                    select(Position).where(
                        Position.user_id == master.id, Position.quantity > ZERO
                    )
                )
                for position in positions:
                    bid = self.feed.get_quote(position.ticker).bid
                    market_value = money(market_value + position.quantity * bid)
                    unrealized = money(
                        unrealized
                        + position.quantity * (bid - position.avg_entry_price)
                    )
                deposits = money(
                    await session.scalar(
                        select(func.coalesce(func.sum(Transaction.amount), ZERO)).where(
                            Transaction.user_id == master.id,
                            Transaction.type == "DEPOSIT",
                        )
                    )
                    or ZERO
                )
                trades = int(
                    await session.scalar(
                        select(func.count(Order.id)).where(
                            Order.user_id == master.id,
                            Order.status == "FILLED",
                            Order.is_copy.is_(False),
                        )
                    )
                    or 0
                )
                subscribers = int(
                    await session.scalar(
                        select(func.count(CopySubscription.id)).where(
                            CopySubscription.master_id == master.id,
                            CopySubscription.active.is_(True),
                        )
                    )
                    or 0
                )
                equity = money(
                    portfolio.cash_balance + portfolio.frozen_balance + market_value
                )
                roi = (
                    money((equity - deposits) / deposits * 100)
                    if deposits > ZERO
                    else ZERO
                )
                result.append(
                    MasterStats(
                        user_id=master.id,
                        username=master.username,
                        equity=equity,
                        total_deposits=deposits,
                        realized_pnl=money(portfolio.realized_pnl),
                        unrealized_pnl=unrealized,
                        trades_count=trades,
                        subscribers_count=subscribers,
                        roi_percent=roi,
                    )
                )
            return result

    async def subscribe(
        self, slave_id: int, data: CopySubscribeRequest
    ) -> SubscriptionResponse:
        async with self._lock, SessionLocal() as session:
            master = await session.get(User, data.master_id)
            if master is None:
                raise BusinessError(
                    "MASTER_NOT_FOUND", "Master user does not exist.", 404
                )
            if master.role != "MASTER":
                raise BusinessError(
                    "NOT_A_MASTER", "The given user does not have the MASTER role."
                )
            slave = await session.get(User, slave_id)
            if slave is None:
                raise BusinessError("USER_NOT_FOUND", "User does not exist.", 404)
            if slave.role != "SLAVE":
                raise BusinessError(
                    "ROLE_NOT_ALLOWED", "Only SLAVE users can subscribe.", 403
                )
            if slave.id == master.id:
                raise BusinessError(
                    "CANNOT_COPY_SELF", "A user cannot copy his own trades."
                )

            duplicate = await session.scalar(
                select(CopySubscription.id).where(
                    CopySubscription.slave_id == slave_id,
                    CopySubscription.master_id == master.id,
                    CopySubscription.active.is_(True),
                )
            )
            if duplicate is not None:
                raise BusinessError(
                    "SUBSCRIPTION_ALREADY_EXISTS",
                    "An active subscription to this master already exists.",
                    409,
                    details={"subscription_id": duplicate, "master_id": master.id},
                )

            allocation = money(data.allocation_usdt)
            portfolio = await self._portfolio(session, slave_id)
            total_balance = money(portfolio.cash_balance + portfolio.frozen_balance)
            if allocation > total_balance:
                raise BusinessError(
                    "INSUFFICIENT_FUNDS",
                    "Allocation exceeds the slave's total balance.",
                    details={
                        "allocation": str(allocation),
                        "available": str(total_balance),
                    },
                )

            subscription = CopySubscription(
                slave_id=slave_id,
                master_id=master.id,
                allocation_usdt=allocation,
                copy_mode=data.copy_mode,
                fixed_ratio=(
                    money(data.fixed_ratio) if data.fixed_ratio is not None else None
                ),
                max_slippage_percent=money(data.max_slippage_percent),
                active=True,
            )
            session.add(subscription)
            await session.commit()
            return SubscriptionResponse.model_validate(subscription)

    async def update_subscription(
        self, slave_id: int, subscription_id: int, data: CopySubscriptionUpdate
    ) -> SubscriptionResponse:
        """Change parameters of an active subscription in place.

        The subscription id and creation time are preserved; switching to a
        different master still requires a new ``POST /subscribe``.
        """
        async with self._lock, SessionLocal() as session:
            subscription = await session.get(CopySubscription, subscription_id)
            if subscription is None:
                raise BusinessError(
                    "SUBSCRIPTION_NOT_FOUND", "Subscription does not exist.", 404
                )
            if subscription.slave_id != slave_id:
                raise BusinessError(
                    "FORBIDDEN", "Subscription belongs to another user.", 403
                )
            if not subscription.active:
                raise BusinessError(
                    "SUBSCRIPTION_NOT_ACTIVE", "Subscription is not active.", 409
                )

            copy_mode = data.copy_mode or subscription.copy_mode
            if copy_mode == "PROPORTIONAL":
                fixed_ratio = None
            elif data.fixed_ratio is not None:
                fixed_ratio = data.fixed_ratio
            elif data.copy_mode is None:
                fixed_ratio = subscription.fixed_ratio
            else:
                fixed_ratio = None
            if copy_mode == "FIXED_RATIO" and fixed_ratio is None:
                raise BusinessError(
                    "FIXED_RATIO_REQUIRED",
                    "fixed_ratio is required when copy_mode is FIXED_RATIO.",
                )

            allocation = subscription.allocation_usdt
            if data.allocation_usdt is not None:
                allocation = money(data.allocation_usdt)
                portfolio = await self._portfolio(session, slave_id)
                total_balance = money(portfolio.cash_balance + portfolio.frozen_balance)
                if allocation > total_balance:
                    raise BusinessError(
                        "INSUFFICIENT_FUNDS",
                        "Allocation exceeds the slave's total balance.",
                        details={
                            "allocation": str(allocation),
                            "available": str(total_balance),
                        },
                    )

            subscription.allocation_usdt = allocation
            subscription.copy_mode = copy_mode
            subscription.fixed_ratio = fixed_ratio
            if data.max_slippage_percent is not None:
                subscription.max_slippage_percent = money(data.max_slippage_percent)
            subscription.updated_at = now()
            await session.commit()
            return SubscriptionResponse.model_validate(subscription)

    async def list_subscriptions(self, slave_id: int) -> list[SubscriptionResponse]:
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(CopySubscription)
                .where(CopySubscription.slave_id == slave_id)
                .order_by(CopySubscription.id.desc())
            )
            return [SubscriptionResponse.model_validate(row) for row in rows]

    async def unsubscribe(self, slave_id: int, subscription_id: int) -> None:
        async with self._lock, SessionLocal() as session:
            subscription = await session.get(CopySubscription, subscription_id)
            if subscription is None:
                raise BusinessError(
                    "SUBSCRIPTION_NOT_FOUND", "Subscription does not exist.", 404
                )
            if subscription.slave_id != slave_id:
                raise BusinessError(
                    "FORBIDDEN", "Subscription belongs to another user.", 403
                )
            subscription.active = False
            subscription.updated_at = now()
            await session.commit()

    async def get_copy_history(
        self, user_id: int, limit: int = 50
    ) -> list[CopyHistoryItem]:
        async with SessionLocal() as session:
            rows = await session.scalars(
                select(CopyTradeHistory)
                .where(
                    (CopyTradeHistory.slave_id == user_id)
                    | (CopyTradeHistory.master_id == user_id)
                )
                .order_by(CopyTradeHistory.id.desc())
                .limit(limit)
            )
            return [CopyHistoryItem.model_validate(row) for row in rows]

    def _schedule_copy(self, snapshot: dict[str, Any]) -> None:
        task = asyncio.create_task(
            self._execute_copy(snapshot), name="copy-master-order"
        )
        self._copy_tasks.add(task)
        task.add_done_callback(self._copy_tasks.discard)

    async def _execute_copy(self, snapshot: dict[str, Any]) -> None:
        """Asynchronously mirror a Master fill for every active subscriber."""
        await asyncio.sleep(settings.copy_delay_ms / 1000)
        async with self._lock, SessionLocal() as session:
            subscriptions = await session.scalars(
                select(CopySubscription).where(
                    CopySubscription.master_id == snapshot["master_id"],
                    CopySubscription.active.is_(True),
                )
            )
            events: list[Any] = []
            for subscription in subscriptions:
                slave = await session.get(User, subscription.slave_id)
                if slave is None:
                    continue

                quote = self.feed.get_quote(snapshot["ticker"])
                copy_price = quote.ask if snapshot["side"] == "BUY" else quote.bid
                master_price = money(snapshot["price"])
                slippage = money(abs(copy_price - master_price) / master_price * 100)

                if subscription.copy_mode == "FIXED_RATIO":
                    ratio = money(subscription.fixed_ratio or ZERO)
                else:
                    equity = money(snapshot["master_equity"])
                    ratio = (
                        money(subscription.allocation_usdt / equity)
                        if equity > ZERO
                        else ZERO
                    )
                    ratio = min(ratio, Decimal("1"))

                amount = money(snapshot["amount"] * ratio)
                notional = money(amount * copy_price)
                spent = money(
                    await session.scalar(
                        select(
                            func.coalesce(func.sum(CopyTradeHistory.notional), ZERO)
                        ).where(
                            CopyTradeHistory.subscription_id == subscription.id,
                            CopyTradeHistory.status == "EXECUTED",
                        )
                    )
                    or ZERO
                )

                skip_reason: str | None = None
                details: dict[str, Any] = {
                    "slippage_percent": str(slippage),
                    "max_slippage_percent": str(
                        money(subscription.max_slippage_percent)
                    ),
                    "master_price": str(master_price),
                    "copy_price": str(copy_price),
                }
                if slippage > subscription.max_slippage_percent:
                    skip_reason = "SLIPPAGE_EXCEEDED"
                elif amount < settings.min_order_amount:
                    skip_reason = "INSUFFICIENT_FUNDS"
                    details["reason"] = "copy amount is below the minimum"
                elif spent + notional > subscription.allocation_usdt:
                    skip_reason = "INSUFFICIENT_FUNDS"
                    details.update(
                        {
                            "allocated": str(money(subscription.allocation_usdt)),
                            "spent": str(spent),
                            "required": str(notional),
                        }
                    )
                else:
                    portfolio = await self._portfolio(session, slave.id)
                    if snapshot["side"] == "BUY" and portfolio.cash_balance < notional:
                        skip_reason = "INSUFFICIENT_FUNDS"
                        details.update(
                            {
                                "required": str(notional),
                                "available": str(money(portfolio.cash_balance)),
                            }
                        )
                    elif snapshot["side"] == "SELL":
                        position = await session.scalar(
                            select(Position).where(
                                Position.user_id == slave.id,
                                Position.ticker == snapshot["ticker"],
                            )
                        )
                        if position is None or position.quantity < amount:
                            skip_reason = "INSUFFICIENT_FUNDS"
                            details.update(
                                {
                                    "required": str(amount),
                                    "available": (
                                        str(money(position.quantity))
                                        if position
                                        else "0.00000000"
                                    ),
                                }
                            )

                history = CopyTradeHistory(
                    subscription_id=subscription.id,
                    master_id=snapshot["master_id"],
                    slave_id=slave.id,
                    master_order_id=snapshot["master_order_id"],
                    ticker=snapshot["ticker"],
                    amount=amount,
                    master_price=master_price,
                    copy_price=copy_price,
                    notional=notional,
                    slippage_percent=slippage,
                    status="SKIPPED" if skip_reason else "EXECUTED",
                    skip_reason=skip_reason,
                )
                session.add(history)
                await session.flush()

                if skip_reason is not None:
                    events.append(
                        CopyTradeResultEvent(
                            user_id=slave.id,
                            master_id=snapshot["master_id"],
                            master_order_id=snapshot["master_order_id"],
                            slave_order_id=None,
                            ticker=snapshot["ticker"],
                            status="SKIPPED",
                            reason=skip_reason,
                            details=details,
                            timestamp=now(),
                        )
                    )
                else:
                    copy_order = Order(
                        user_id=slave.id,
                        ticker=snapshot["ticker"],
                        side=snapshot["side"],
                        order_type="LIMIT",
                        amount=amount,
                        filled_qty=ZERO,
                        remaining_qty=amount,
                        min_price=copy_price,
                        max_price=copy_price,
                        time_in_force="IOC",
                        status="NEW",
                        reserved_amount=ZERO,
                        is_copy=True,
                        copy_of_order_id=snapshot["master_order_id"],
                    )
                    session.add(copy_order)
                    await session.flush()
                    events.extend(
                        await self._fill_order_locked(session, copy_order, copy_price)
                    )
                    history.slave_order_id = copy_order.id
                    events.append(
                        CopyTradeResultEvent(
                            user_id=slave.id,
                            master_id=snapshot["master_id"],
                            master_order_id=snapshot["master_order_id"],
                            slave_order_id=copy_order.id,
                            ticker=snapshot["ticker"],
                            status="EXECUTED",
                            reason=None,
                            details={"notional": str(notional), "amount": str(amount)},
                            timestamp=now(),
                        )
                    )

            await session.commit()
            for event in events:
                self._publish_to_user(event.user_id, event)

    async def shutdown(self) -> None:
        for task in list(self._copy_tasks):
            task.cancel()
        if self._copy_tasks:
            await asyncio.gather(*self._copy_tasks, return_exceptions=True)
        self._copy_tasks.clear()
