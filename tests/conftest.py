"""Session-wide SUT lifecycle and per-test isolated user fixtures."""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import pytest

from src.clients.trading_api import TradingAPIClient
from src.clients.validators import expect_model, expect_status
from src.constants import Role
from src.fixtures import unique_username
from src.schemas.auth import LoginResponse, UserResponse
from tests.config import settings as qa

os.environ["TRADING_DATABASE_URL"] = f"sqlite+aiosqlite:///./{qa.test_db}"
os.environ["TRADING_JWT_SECRET"] = qa.jwt_secret
os.environ["TRADING_JWT_ALGORITHM"] = qa.jwt_algorithm
os.environ["TRADING_COPY_DELAY_MS"] = str(qa.copy_delay_ms)
os.environ["TRADING_MARKET_TICK_INTERVAL_MS"] = str(qa.tick_interval_ms)
os.environ["TRADING_MARKET_REGIME"] = qa.market_regime


@dataclass(frozen=True)
class Runtime:
    host: str
    port: int
    base_url: str
    ws_url: str
    db_path: str


@dataclass
class UserContext:
    """Зарегистрированный пользователь с готовым api клиентом"""

    id: int
    username: str
    role: Role
    token: str
    client: TradingAPIClient


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def runtime() -> Runtime:
    for suffix in ("", "-wal", "-shm"):
        path = Path(qa.test_db + suffix)
        if path.exists():
            path.unlink()
    port = qa.server_port or _free_port()
    return Runtime(
        host=qa.server_host,
        port=port,
        base_url=f"http://{qa.server_host}:{port}",
        ws_url=f"ws://{qa.server_host}:{port}",
        db_path=qa.test_db,
    )


@pytest.fixture(scope="session")
def server(runtime: Runtime):
    import uvicorn

    from app.main import app

    config = uvicorn.Config(
        app,
        host=runtime.host,
        port=runtime.port,
        log_level="warning",
        access_log=False,
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, name="qa-sut", daemon=True)
    thread.start()

    deadline = time.monotonic() + qa.server_start_timeout
    with httpx.Client(base_url=runtime.base_url, timeout=1.0) as probe:
        while time.monotonic() < deadline:
            try:
                if probe.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(qa.poll_interval)
        else:
            instance.should_exit = True
            thread.join(timeout=qa.server_stop_timeout)
            pytest.fail("SUT не ответил на health check до истечения таймаута")

    yield instance

    instance.should_exit = True
    thread.join(timeout=qa.server_stop_timeout)


@pytest.fixture
async def api(runtime: Runtime, server) -> TradingAPIClient:
    async with TradingAPIClient(runtime.base_url, timeout=qa.request_timeout) as client:
        yield client


@pytest.fixture
async def user_factory(
    runtime: Runtime, server
) -> Callable[..., Awaitable[UserContext]]:

    clients: list[TradingAPIClient] = []

    async def _create_user(
        role: Role,
        *,
        amount: str | None = None,
    ) -> UserContext:
        username = unique_username(role.lower())
        async with TradingAPIClient(
            runtime.base_url, timeout=qa.request_timeout
        ) as api_client:
            response = await api_client.register(username, qa.password, role)
            user = expect_model(response, UserResponse, expected=201)

            response = await api_client.login(username, qa.password)
            token = expect_model(response, LoginResponse, expected=200).access_token

        client = TradingAPIClient(
            runtime.base_url, token=token, timeout=qa.request_timeout
        )
        clients.append(client)
        context = UserContext(
            id=user.id, username=username, role=role, token=token, client=client
        )
        if amount is not None:
            response = await client.deposit(amount)
            expect_status(response, 200)

        return context

    yield _create_user

    for client in clients:
        await client.aclose()


@pytest.fixture
async def master(user_factory) -> UserContext:
    return await user_factory(Role.MASTER, amount=qa.master_deposit)


@pytest.fixture
async def slave(user_factory) -> UserContext:
    return await user_factory(Role.SLAVE, amount=qa.slave_deposit)
