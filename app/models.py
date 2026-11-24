"""SQLAlchemy 2.0 ORM models backed by ``trading.db``.

All monetary columns use ``NUMERIC(20, 8)``; values are represented as
``decimal.Decimal`` in Python so no binary-float drift is introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .config import ZERO
from .database import Base

Money = Numeric(20, 8)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # MASTER | SLAVE
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    portfolio: Mapped[Portfolio | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    orders: Mapped[list[Order]] = relationship(back_populates="user")
    positions: Mapped[list[Position]] = relationship(back_populates="user")


class Portfolio(Base):
    __tablename__ = "portfolios"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    cash_balance: Mapped[Decimal] = mapped_column(Money, default=ZERO, nullable=False)
    frozen_balance: Mapped[Decimal] = mapped_column(Money, default=ZERO, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Money, default=ZERO, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="portfolio")


class Transaction(Base):
    """Immutable ledger of deposits, order settlements and closes."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Money, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker", name="uq_position_user_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, default=ZERO, nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(
        Money, default=ZERO, nullable=False
    )
    stop_loss: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    source_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped[User] = relationship(back_populates="positions")


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_status_ticker", "status", "ticker"),
        Index("ix_orders_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY | SELL
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    min_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    max_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False)  # IOC | GTC
    stop_loss: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", nullable=False)
    filled_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    reserved_amount: Mapped[Decimal] = mapped_column(
        Money, default=ZERO, nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_copy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    copy_of_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    filled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="orders")


class CopySubscription(Base):
    __tablename__ = "copy_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slave_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    master_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allocation_usdt: Mapped[Decimal] = mapped_column(Money, nullable=False)
    copy_mode: Mapped[str] = mapped_column(
        String(24), nullable=False
    )  # PROPORTIONAL | FIXED_RATIO
    fixed_ratio: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    max_slippage_percent: Mapped[Decimal] = mapped_column(Money, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CopyTradeHistory(Base):
    """Audit log for every copy attempt (successful or skipped)."""

    __tablename__ = "copy_trade_history"
    __table_args__ = (Index("ix_copy_history_master_slave", "master_id", "slave_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("copy_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    master_id: Mapped[int] = mapped_column(Integer, nullable=False)
    slave_id: Mapped[int] = mapped_column(Integer, nullable=False)
    master_order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    slave_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # EXECUTED | SKIPPED
    skip_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    master_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    copy_price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    notional: Mapped[Decimal] = mapped_column(Money, default=ZERO, nullable=False)
    slippage_percent: Mapped[Decimal] = mapped_column(
        Money, default=ZERO, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
