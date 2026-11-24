"""WebSocket connection registry with non-blocking, ordered outbound queues."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ClientConnection:
    """One socket + FIFO outbound queue (slow clients never block the engine)."""

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        self._writer = asyncio.create_task(self._send_loop(), name="ws-sender")

    def enqueue(self, message: dict[str, Any]) -> None:
        if not self._closed:
            self._queue.put_nowait(message)

    async def _send_loop(self) -> None:
        try:
            while True:
                message = await self._queue.get()
                await self._websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("WebSocket sender stopped: %r", exc)
            self._closed = True
            with contextlib.suppress(Exception):
                await self._websocket.close()

    async def stop(self) -> None:
        self._closed = True
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer
            self._writer = None


class ConnectionManager:
    """Manages public market channels and private per-user channels.

    Registry mutations are synchronous and await-free, so they are atomic with
    respect to the event loop; no additional lock is needed and the trading
    engine can publish events while holding its own DB write lock.
    """

    def __init__(self) -> None:
        self._market: dict[ClientConnection, set[str]] = {}
        self._users: dict[int, set[ClientConnection]] = {}

    # -- market channel ----------------------------------------------------
    def attach_market(self, websocket: WebSocket) -> ClientConnection:
        connection = ClientConnection(websocket)
        connection.start()
        self._market[connection] = set()
        return connection

    def set_tickers(self, connection: ClientConnection, tickers: list[str]) -> None:
        if connection in self._market:
            self._market[connection] = set(tickers)

    def add_tickers(self, connection: ClientConnection, tickers: list[str]) -> None:
        if connection in self._market:
            self._market[connection].update(tickers)

    def remove_tickers(self, connection: ClientConnection, tickers: list[str]) -> None:
        if connection in self._market:
            self._market[connection].difference_update(tickers)

    def detach_market(self, connection: ClientConnection) -> None:
        self._market.pop(connection, None)
        asyncio.create_task(connection.stop())

    def broadcast_tick(self, ticker: str, payload: dict[str, Any]) -> None:
        for connection, tickers in list(self._market.items()):
            if connection.closed:
                self._market.pop(connection, None)
                asyncio.create_task(connection.stop())
            elif ticker in tickers:
                connection.enqueue(payload)

    # -- private user channels --------------------------------------------
    def attach_user(self, user_id: int, websocket: WebSocket) -> ClientConnection:
        connection = ClientConnection(websocket)
        connection.start()
        self._users.setdefault(user_id, set()).add(connection)
        return connection

    def detach_user(self, user_id: int, connection: ClientConnection) -> None:
        connections = self._users.get(user_id)
        if connections is not None:
            connections.discard(connection)
            if not connections:
                self._users.pop(user_id, None)
        asyncio.create_task(connection.stop())

    def publish_user(self, user_id: int, payload: dict[str, Any]) -> None:
        connections = self._users.get(user_id)
        if not connections:
            return
        for connection in list(connections):
            if connection.closed:
                connections.discard(connection)
            else:
                connection.enqueue(payload)
        if not connections:
            self._users.pop(user_id, None)
