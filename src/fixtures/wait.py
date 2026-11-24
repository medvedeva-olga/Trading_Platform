import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

T = TypeVar("T")

Predicate = Callable[[], T | None | Awaitable[T | None]]


async def wait_until(
    predicate: Predicate[T],
    *,
    timeout: float,
    interval: float,
    description: str,
) -> T:
    """Return the first truthy predicate result, or fail after ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: T | None = None
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result  # type: ignore[return-value]
        last = result  # type: ignore[assignment]
        await asyncio.sleep(interval)
    pytest.fail(
        f"Таймаут {timeout} с при ожидании: {description} "
        f"(последнее значение: {last!r})"
    )
