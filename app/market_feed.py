"""Background market data simulator (random walk + bid/ask spread)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .config import INITIAL_PRICES, TICKERS, money, settings

logger = logging.getLogger(__name__)

TickCallback = Callable[[str, "Quote"], Awaitable[None]]


@dataclass(frozen=True)
class Quote:
    ticker: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    spread: Decimal
    timestamp: datetime


DepthSide = tuple[tuple[Decimal, Decimal], ...]


class MarketFeed:
    """Emits a quote for every configured ticker every ``tick_interval_ms``."""

    def __init__(self) -> None:
        self._prices: dict[str, Decimal] = {
            t: money(INITIAL_PRICES[t]) for t in TICKERS
        }
        self._quotes: dict[str, Quote] = {}
        self._callbacks: list[TickCallback] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._rng = random.Random(settings.market_seed)
        self._depth_overrides: dict[str, tuple[DepthSide, DepthSide]] = {}
        self._refresh_quotes()

    def subscribe(self, callback: TickCallback) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="market-feed")
            logger.info(
                "Market feed started (interval=%dms, volatility=%s)",
                settings.market_tick_interval_ms,
                settings.volatility,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_quote(self, ticker: str) -> Quote:
        quote = self._quotes.get(ticker)
        if quote is None:
            raise KeyError(f"Unknown ticker: {ticker}")
        return quote

    def get_quotes(self) -> dict[str, Quote]:
        return dict(self._quotes)

    def get_depth(self, ticker: str) -> tuple[DepthSide, DepthSide]:
        """Return synthetic (or QA-overridden) book levels for ``ticker``."""
        if ticker not in self._quotes:
            raise KeyError(f"Unknown ticker: {ticker}")
        override = self._depth_overrides.get(ticker)
        if override is not None:
            return override
        quote = self._quotes[ticker]
        return self._synthetic_depth(quote)

    def set_depth(
        self,
        ticker: str,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
    ) -> None:
        if ticker not in self._quotes:
            raise KeyError(f"Unknown ticker: {ticker}")
        self._depth_overrides[ticker] = (tuple(bids), tuple(asks))

    def reset_depth(self, ticker: str | None = None) -> None:
        if ticker is None:
            self._depth_overrides.clear()
        else:
            self._depth_overrides.pop(ticker, None)

    @staticmethod
    def _synthetic_depth(quote: Quote) -> tuple[DepthSide, DepthSide]:
        # Keep synthetic levels inside a 1 bp band so a corridor starting just
        # above/below the current quote stays resting until the market moves.
        step = max(money(quote.last * Decimal("0.00001")), Decimal("0.00000001"))
        quantity = money(settings.synthetic_depth_qty)
        bids = tuple(
            (money(quote.bid - i * step), quantity)
            for i in range(settings.depth_levels)
        )
        asks = tuple(
            (money(quote.ask + i * step), quantity)
            for i in range(settings.depth_levels)
        )
        return bids, asks

    def _refresh_quotes(self) -> None:
        for ticker, last in self._prices.items():
            spread = money(last * settings.spread_bps / 10_000)
            self._quotes[ticker] = Quote(
                ticker=ticker,
                bid=money(last - spread / 2),
                ask=money(last + spread / 2),
                last=money(last),
                spread=money(spread),
                timestamp=datetime.now(timezone.utc),
            )

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(settings.market_tick_interval_ms / 1000)
                async with self._lock:
                    volatility = settings.volatility
                    # Keep the random step in Decimal space so no binary float
                    # ever touches money values (random.uniform returns float).
                    delta = Decimal(
                        str(self._rng.uniform(-float(volatility), float(volatility)))
                    )
                    for ticker, last in list(self._prices.items()):
                        next_price = money(last * (Decimal(1) + delta))
                        if next_price <= 0:
                            next_price = money("0.01000000")
                        self._prices[ticker] = next_price
                    self._refresh_quotes()
                    quotes = tuple(self._quotes.values())

                for quote in quotes:
                    for callback in self._callbacks:
                        try:
                            await callback(quote.ticker, quote)
                        except Exception:  # a broken consumer must not kill the feed
                            logger.exception(
                                "Market feed callback failed for %s", quote.ticker
                            )
        except asyncio.CancelledError:
            logger.info("Market feed stopped")
            raise
