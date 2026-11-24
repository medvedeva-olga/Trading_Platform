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
