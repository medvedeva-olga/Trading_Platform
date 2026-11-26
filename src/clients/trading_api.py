import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
import websockets

from src.constants import StreamAction


def to_ws_url(base_url: str) -> str:
    url = httpx.URL(base_url)
    return str(url.copy_with(scheme="wss" if url.scheme == "https" else "ws"))


class TradingAPIClient:

    def __init__(
        self, base_url: str, token: str | None = None, timeout: float = 10.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def __aenter__(self) -> TradingAPIClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self):
        await self._http.aclose()

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def register(self, username: str, password: str, role: str) -> httpx.Response:
        return await self._http.post(
            "/api/v1/auth/register",
            json={"username": username, "password": password, "role": role},
        )

    async def login(self, username: str, password: str) -> httpx.Response:
        return await self._http.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )

    async def deposit(self, amount: str) -> httpx.Response:
        return await self._http.post(
            "/api/v1/portfolio/deposit", headers=self.headers, json={"amount": amount}
        )

    async def portfolio_summary(self) -> httpx.Response:
        return await self._http.get("/api/v1/portfolio/summary", headers=self.headers)

    async def exposure(self) -> httpx.Response:
        return await self._http.get("/api/v1/portfolio/exposure", headers=self.headers)

    async def tickers(self) -> httpx.Response:
        return await self._http.get("/api/v1/market/tickers", headers=self.headers)

    async def depth(self, ticker: str, limit: int = 5) -> httpx.Response:
        return await self._http.get(
            "/api/v1/market/depth",
            headers=self.headers,
            params={"ticker": ticker, "limit": limit},
        )

    async def set_depth(self, payload: dict[str, Any]) -> httpx.Response:
        return await self._http.post("/api/v1/test/depth", json=payload)

    async def reset_depth(self, ticker: str | None = None) -> httpx.Response:
        params = {"ticker": ticker} if ticker else None
        return await self._http.delete("/api/v1/test/depth", params=params)

    async def create_order(self, payload: dict[str, Any]) -> httpx.Response:
        return await self._http.post(
            "/api/v1/orders", headers=self.headers, json=payload
        )

    async def get_order(self, order_id: int) -> httpx.Response:
        return await self._http.get(f"/api/v1/orders/{order_id}", headers=self.headers)

    async def cancel_order(self, order_id: int) -> httpx.Response:
        return await self._http.delete(
            f"/api/v1/orders/{order_id}", headers=self.headers
        )

    async def masters(self) -> httpx.Response:
        return await self._http.get(
            "/api/v1/copy-trading/masters", headers=self.headers
        )

    async def subscribe(self, payload: dict[str, Any]) -> httpx.Response:
        return await self._http.post(
            "/api/v1/copy-trading/subscribe", headers=self.headers, json=payload
        )

    async def subscriptions(self) -> httpx.Response:
        return await self._http.get(
            "/api/v1/copy-trading/subscriptions", headers=self.headers
        )

    async def update_subscription(
        self, subscription_id: int, payload: dict[str, Any]
    ) -> httpx.Response:
        return await self._http.patch(
            f"/api/v1/copy-trading/subscriptions/{subscription_id}",
            headers=self.headers,
            json=payload,
        )

    async def unsubscribe(self, subscription_id: int) -> httpx.Response:
        return await self._http.delete(
            f"/api/v1/copy-trading/subscriptions/{subscription_id}",
            headers=self.headers,
        )

    async def copy_history(self, limit: int = 50) -> httpx.Response:
        return await self._http.get(
            "/api/v1/copy-trading/history",
            params={"limit": limit},
            headers=self.headers,
        )


@asynccontextmanager
async def open_market_stream(base_url: str, tickers: list[str] | None = None):
    async with websockets.connect(f"{to_ws_url(base_url)}/ws/market") as socket:
        if tickers is not None:
            payload = json.dumps({"action": StreamAction.SET, "tickers": tickers})
            await socket.send(payload)
        yield socket


@asynccontextmanager
async def open_user_stream(base_url: str, token: str):
    async with websockets.connect(
        f"{to_ws_url(base_url)}/ws/user?token={quote(token)}"
    ) as socket:
        yield socket


async def recv_json(socket: Any, timeout: float | None = None) -> dict[str, Any]:
    if timeout is None:
        raw = await socket.recv()
    else:
        raw = await asyncio.wait_for(socket.recv(), timeout)
    return json.loads(raw)
