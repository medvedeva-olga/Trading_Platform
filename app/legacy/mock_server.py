"""In-memory mock trading server for API/WebSocket test automation.

Features
--------
* ``POST /api/v1/orders``               - instant order execution (BUY/SELL).
* ``GET  /api/v1/accounts/{id}/exposure`` - current account exposure snapshot.
* ``POST /api/v1/reset``                - wipe all in-memory state (test isolation).
* ``WS   /ws/exposure/{id}``            - live exposure stream (snapshot + updates).

All state lives in process memory and is guarded by a single ``asyncio.Lock``.
External dependencies: fastapi, uvicorn, pydantic v2.

Run:
    python mock_server.py
    # or
    uvicorn mock_server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import FastAPI, Path, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

logger = logging.getLogger("mock_trading_server")

OrderSide = Literal["BUY", "SELL"]


# ---------------------------------------------------------------------------
# Time helpers & shared annotated types
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def serialize_datetime(value: datetime) -> str:
    """Serialize to ISO 8601 with a trailing ``Z`` (e.g. 2026-11-18T10:00:00.000Z)."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


UtcTimestamp = Annotated[
    datetime,
    PlainSerializer(serialize_datetime, return_type=str, when_used="json"),
]


# ---------------------------------------------------------------------------
# Pydantic models (API contract)
# ---------------------------------------------------------------------------
class OrderCreate(BaseModel):
    """Тело запроса на создание торговой заявки."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "account_id": "ACC-123",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "quantity": 10,
                    "price": 150.05,
                }
            ]
        }
    )

    account_id: str = Field(
        min_length=1,
        max_length=64,
        examples=["ACC-123"],
        description="Идентификатор счета (1-64 символа).",
    )
    symbol: str = Field(
        min_length=1,
        max_length=32,
        examples=["AAPL"],
        description="Тикер инструмента (1-32 символа), например `AAPL`.",
    )
    side: OrderSide = Field(
        description="Направление сделки: `BUY` или `SELL` (регистр важен)."
    )
    quantity: int = Field(
        gt=0, examples=[10], description="Количество бумаг, строго больше 0."
    )
    price: float = Field(
        gt=0,
        allow_inf_nan=False,
        examples=[150.05],
        description="Цена мгновенного исполнения, строго больше 0.",
    )


class OrderResponse(BaseModel):
    """Подтверждение мгновенно исполненной заявки."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "order_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "status": "FILLED",
                    "timestamp": "2026-11-18T10:00:00.000Z",
                    "account_id": "ACC-123",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "quantity": 10,
                    "price": 150.05,
                }
            ]
        }
    )

    order_id: uuid.UUID = Field(description="Уникальный идентификатор заявки (UUID4).")
    status: Literal["FILLED"] = Field(
        default="FILLED", description="Статус заявки; всегда `FILLED`."
    )
    timestamp: UtcTimestamp = Field(
        description="Время исполнения в формате ISO 8601 (UTC, суффикс `Z`)."
    )
    account_id: str = Field(description="Идентификатор счета из запроса.")
    symbol: str = Field(description="Тикер из запроса.")
    side: OrderSide = Field(description="Направление сделки из запроса.")
    quantity: int = Field(description="Количество бумаг из запроса.")
    price: float = Field(description="Цена исполнения из запроса.")


class AccountExposure(BaseModel):
    """Текущее состояние счета (REST)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "account_id": "ACC-123",
                    "total_exposure": 1500.5,
                    "positions": {"AAPL": 10},
                }
            ]
        }
    )

    account_id: str = Field(description="Идентификатор счета.")
    total_exposure: float = Field(
        description="Суммарная рыночная стоимость открытых позиций: `sum(quantity * price)` с учетом знака."
    )
    positions: dict[str, int] = Field(
        description="Открытые позиции по тикерам: `{symbol: quantity}`. Нулевые позиции сохраняются."
    )


class ExposureSnapshot(BaseModel):
    """Первое WebSocket-сообщение сразу после подключения клиента."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "EXPOSURE_SNAPSHOT",
                    "account_id": "ACC-123",
                    "total_exposure": 1505.0,
                    "positions": {"AAPL": 10},
                    "timestamp": "2026-11-18T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["EXPOSURE_SNAPSHOT"] = Field(
        default="EXPOSURE_SNAPSHOT", description="Тип события: `EXPOSURE_SNAPSHOT`."
    )
    account_id: str = Field(description="Идентификатор счета.")
    total_exposure: float = Field(description="Текущая суммарная экспозиция счета.")
    positions: dict[str, int] = Field(
        description="Текущие позиции по тикерам: `{symbol: quantity}`."
    )
    timestamp: UtcTimestamp = Field(
        description="Время формирования snapshot (ISO 8601, UTC)."
    )


class ExposureUpdate(BaseModel):
    """WebSocket-событие, отправляемое после каждого исполненного ордера."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event": "EXPOSURE_UPDATE",
                    "account_id": "ACC-123",
                    "total_exposure": 1500.5,
                    "delta": 500.5,
                    "last_order_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "timestamp": "2026-11-18T10:00:00.000Z",
                }
            ]
        }
    )

    event: Literal["EXPOSURE_UPDATE"] = Field(
        default="EXPOSURE_UPDATE", description="Тип события: `EXPOSURE_UPDATE`."
    )
    account_id: str = Field(
        description="Идентификатор счета, по которому изменилась экспозиция."
    )
    total_exposure: float = Field(
        description="Экспозиция счета после исполнения заявки."
    )
    delta: float = Field(
        description="Изменение экспозиции: `+quantity * price` для `BUY`, `-quantity * price` для `SELL`."
    )
    last_order_id: uuid.UUID = Field(
        description="Идентификатор заявки, вызвавшей изменение."
    )
    timestamp: UtcTimestamp = Field(description="Время события (ISO 8601, UTC).")


class ResetResponse(BaseModel):
    """Результат сброса состояния."""

    status: Literal["ok"] = Field(default="ok", description="Всегда `ok`.")
    accounts_cleared: int = Field(description="Количество удаленных счетов.")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


# ---------------------------------------------------------------------------
# WebSocket connection wrapper
# ---------------------------------------------------------------------------
class ClientConnection:
    """Single subscriber with an outbound queue.

    Sending through a queue keeps ``TradingState`` non-blocking (a slow or dead
    client never stalls order processing) and guarantees FIFO ordering, so the
    initial snapshot is always delivered before subsequent updates.
    """

    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        self._writer = asyncio.create_task(self._send_loop(), name="ws-exposure-sender")

    def enqueue(self, message: dict[str, Any]) -> None:
        """Non-blocking put; silently ignored once the connection is closed."""
        if not self._closed:
            self._queue.put_nowait(message)

    async def _send_loop(self) -> None:
        try:
            while True:
                message = await self._queue.get()
                await self._websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # client vanished, socket already closed, etc.
            logger.debug("WebSocket send loop stopped: %r", exc)
            self._closed = True
            with contextlib.suppress(Exception):
                await self._websocket.close()

    async def stop(self) -> None:
        """Cancel the sender task and mark the connection as closed."""
        self._closed = True
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._writer
            self._writer = None


# ---------------------------------------------------------------------------
# State & subscription manager
# ---------------------------------------------------------------------------
@dataclass
class AccountState:
    """Mutable in-memory state of a single account."""

    total_exposure: Decimal = field(default_factory=lambda: Decimal("0"))
    positions: dict[str, int] = field(default_factory=dict)


class TradingState:
    """Thread-safe (task-safe) store for accounts and WebSocket subscriptions.

    A single ``asyncio.Lock`` protects both the account ledger and the
    subscription registry, which makes "apply order + queue broadcast to every
    subscriber" an atomic step: no update can be missed between reading the
    snapshot and registering a new subscriber.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._accounts: dict[str, AccountState] = {}
        self._subscribers: dict[str, set[ClientConnection]] = {}

    # -- helpers (must be called with the lock held) ------------------------
    @staticmethod
    def _to_exposure(account_id: str, account: AccountState) -> AccountExposure:
        return AccountExposure(
            account_id=account_id,
            total_exposure=float(account.total_exposure),
            positions=dict(account.positions),
        )

    def _broadcast_locked(self, account_id: str, payload: dict[str, Any]) -> None:
        """Queue a message for every subscriber of ``account_id``.

        Closed connections are pruned lazily on each broadcast.
        """
        connections = self._subscribers.get(account_id)
        if not connections:
            return

        for connection in list(connections):
            if connection.closed:
                connections.discard(connection)
            else:
                connection.enqueue(payload)

        if not connections:
            self._subscribers.pop(account_id, None)

    # -- public API ---------------------------------------------------------
    async def place_order(self, order: OrderCreate) -> OrderResponse:
        """Execute an order instantly and broadcast the resulting exposure."""
        order_id = uuid.uuid4()
        timestamp = utc_now()

        signed_quantity = order.quantity if order.side == "BUY" else -order.quantity
        delta = Decimal(signed_quantity) * Decimal(str(order.price))

        async with self._lock:
            account = self._accounts.setdefault(order.account_id, AccountState())
            account.total_exposure += delta
            account.positions[order.symbol] = (
                account.positions.get(order.symbol, 0) + signed_quantity
            )

            # Keep a single representation of zero (avoids "-0.0" in JSON).
            if account.total_exposure == 0:
                account.total_exposure = Decimal("0")

            event = ExposureUpdate(
                account_id=order.account_id,
                total_exposure=float(account.total_exposure),
                delta=float(delta),
                last_order_id=order_id,
                timestamp=timestamp,
            )
            # Broadcast happens inside the lock but only enqueues (no I/O await),
            # therefore the REST response is returned only after the event was
            # handed to every active subscriber.
            self._broadcast_locked(order.account_id, event.model_dump(mode="json"))

        return OrderResponse(
            order_id=order_id,
            timestamp=timestamp,
            account_id=order.account_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=order.price,
        )

    async def get_exposure(self, account_id: str) -> AccountExposure:
        """Return the current account snapshot; unknown accounts read as zero."""
        async with self._lock:
            account = self._accounts.get(account_id)
            if account is None:
                return AccountExposure(
                    account_id=account_id, total_exposure=0.0, positions={}
                )
            return self._to_exposure(account_id, account)

    async def reset(self) -> int:
        """Clear all account state. Returns the number of accounts removed.

        WebSocket connections stay alive: from the next order onwards they
        receive consistent updates again. Reconnect (or GET the exposure) if a
        fresh snapshot is required after a reset.
        """
        async with self._lock:
            cleared = len(self._accounts)
            self._accounts.clear()
            return cleared

    async def subscribe(
        self, account_id: str, websocket: WebSocket
    ) -> ClientConnection:
        """Register a WebSocket and enqueue the initial snapshot atomically."""
        connection = ClientConnection(websocket)
        connection.start()

        async with self._lock:
            account = self._accounts.get(account_id) or AccountState()
            snapshot = ExposureSnapshot(
                account_id=account_id,
                total_exposure=float(account.total_exposure),
                positions=dict(account.positions),
                timestamp=utc_now(),
            )
            # Snapshot goes first into the FIFO queue and registration happens
            # under the same lock, so no exposure update can overtake it.
            connection.enqueue(snapshot.model_dump(mode="json"))
            self._subscribers.setdefault(account_id, set()).add(connection)

        logger.info(
            "WS subscribed: account=%s total=%d",
            account_id,
            len(self._subscribers.get(account_id, ())),
        )
        return connection

    async def unsubscribe(self, account_id: str, connection: ClientConnection) -> None:
        """Remove a WebSocket subscription and stop its sender task."""
        async with self._lock:
            connections = self._subscribers.get(account_id)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    self._subscribers.pop(account_id, None)
        await connection.stop()
        logger.info("WS unsubscribed: account=%s", account_id)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
API_DESCRIPTION = """
In-memory моковый торговый сервер для автоматизации тестирования REST и WebSocket API.

## Возможности

* Заявки исполняются мгновенно по переданной цене (без стакана и очередей).
* Экспозиция и позиции хранятся в памяти процесса и защищены `asyncio.Lock`.
* После каждого исполненного ордера изменение экспозиции рассылается всем
  WebSocket-подписчикам соответствующего счета.
* `POST /api/v1/reset` полностью очищает состояние — удобно для изоляции автотестов.

## Модель экспозиции

```
BUY:  exposure += quantity * price
SELL: exposure -= quantity * price
```

Позиция по тикеру меняется на `+quantity` для `BUY` и `-quantity` для `SELL`.
Внутренние расчеты выполняются в `Decimal`, наружу значения отдаются как `float`.

## WebSocket-контракт

OpenAPI не описывает WebSocket, поэтому контракт зафиксирован здесь
(и в vendor-расширении `x-websocket-endpoints`).

**URL:** `ws://localhost:8000/ws/exposure/{account_id}`

1. Сразу после подключения сервер отправляет snapshot — схема `ExposureSnapshot`.
2. После каждого `POST /api/v1/orders` по этому счету приходит событие
   `ExposureUpdate` c `event: "EXPOSURE_UPDATE"`.
3. Рассылка изолирована по `account_id`; отключенные клиенты удаляются из реестра.

Пример события:

```json
{
  "event": "EXPOSURE_UPDATE",
  "account_id": "ACC-123",
  "total_exposure": 1500.50,
  "delta": 500.50,
  "last_order_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "timestamp": "2026-11-18T10:00:00.000Z"
}
```

## Типовой сценарий теста

1. `POST /api/v1/reset` — очистить состояние.
2. Подключиться к `ws://.../ws/exposure/{account_id}`, дождаться snapshot.
3. `POST /api/v1/orders` — создать заявку.
4. Проверить `EXPOSURE_UPDATE` из WebSocket и сверить с
   `GET /api/v1/accounts/{account_id}/exposure`.
"""

OPENAPI_TAGS = [
    {
        "name": "trading",
        "description": "Создание и мгновенное исполнение торговых заявок.",
    },
    {
        "name": "risk",
        "description": "Просмотр текущей риск-экспозиции и позиций по счетам.",
    },
    {
        "name": "admin",
        "description": "Служебные операции: сброс состояния и health-check.",
    },
]

app = FastAPI(
    title="Mock Trading Server",
    description=API_DESCRIPTION,
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,
    servers=[
        {
            "url": "http://localhost:8000",
            "description": "Локальный запуск (по умолчанию)",
        },
    ],
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

trading_state = TradingState()


@app.post(
    "/api/v1/orders",
    response_model=OrderResponse,
    status_code=201,
    tags=["trading"],
    summary="Создать и мгновенно исполнить заявку",
    description=(
        "Исполняет заявку по переданной цене, обновляет экспозицию и позицию по тикеру, "
        "затем рассылает `EXPOSURE_UPDATE` подписчикам счета.\n\n"
        "**Ответ 422** возвращается при невалидном теле запроса: "
        "`quantity <= 0`, `price <= 0`, неизвестный `side` и т.п."
    ),
    response_description="Заявка исполнена, возвращены ее поля и присвоенный `order_id`.",
    operation_id="createOrder",
)
async def create_order(order: OrderCreate) -> OrderResponse:
    """Fill the order at the given price and update account exposure."""
    return await trading_state.place_order(order)


@app.get(
    "/api/v1/accounts/{account_id}/exposure",
    response_model=AccountExposure,
    tags=["risk"],
    summary="Получить текущую экспозицию счета",
    description=(
        "Возвращает текущее состояние счета: суммарную экспозицию и позиции по тикерам.\n\n"
        "Для неизвестного счета возвращает `total_exposure = 0.0` и пустой `positions` "
        "(запись в памяти не создается).\n\n"
        "Данные совпадают с первым WebSocket-сообщением (`EXPOSURE_SNAPSHOT`) — удобно "
        "для сверки REST и WS."
    ),
    response_description="Текущее состояние счета.",
    operation_id="getAccountExposure",
)
async def get_exposure(
    account_id: str = Path(description="Идентификатор счета.", examples=["ACC-123"]),
) -> AccountExposure:
    """Same data as the initial WebSocket snapshot - handy for REST/WS assertions."""
    return await trading_state.get_exposure(account_id)


@app.post(
    "/api/v1/reset",
    response_model=ResetResponse,
    tags=["admin"],
    summary="Сбросить все состояние сервера",
    description=(
        "Удаляет все счета, их экспозиции и позиции из памяти процесса.\n\n"
        "Активные WebSocket-соединения не закрываются: следующие заявки снова начнут "
        "присылать консистентные обновления. Для получения свежего snapshot после сброса "
        "переподключитесь или запросите REST-эндпоинт экспозиции."
    ),
    response_description="Количество очищенных счетов.",
    operation_id="resetState",
)
async def reset_state() -> ResetResponse:
    """Drop every account balance/position. Active WebSockets are kept open."""
    cleared = await trading_state.reset()
    return ResetResponse(accounts_cleared=cleared)


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["admin"],
    summary="Проверка доступности сервера",
    description="Liveness-проба. Используйте для ожидания готовности сервера в тестах.",
    operation_id="healthCheck",
)
async def health() -> HealthResponse:
    return HealthResponse()


@app.websocket("/ws/exposure/{account_id}")
async def exposure_stream(websocket: WebSocket, account_id: str) -> None:
    """Stream account exposure: an immediate snapshot, then one event per order."""
    await websocket.accept()
    connection = await trading_state.subscribe(account_id, websocket)
    try:
        while True:
            # We do not expect client payloads; this call only detects disconnects.
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # never leak a broken socket into the registry
        logger.debug("WebSocket receive loop stopped: %r", exc)
    finally:
        await trading_state.unsubscribe(account_id, connection)


# ---------------------------------------------------------------------------
# OpenAPI customization
# ---------------------------------------------------------------------------
# WebSocket messages are not expressible in OpenAPI, so their Pydantic schemas
# are injected into components and referenced from a vendor extension. This
# keeps the generated swagger.json self-describing for QA tooling.
_base_openapi = app.openapi


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema is not None:
        return app.openapi_schema

    schema = _base_openapi()
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in (ExposureSnapshot, ExposureUpdate):
        components.setdefault(
            model.__name__,
            model.model_json_schema(ref_template="#/components/schemas/{model}"),
        )

    schema["x-websocket-endpoints"] = [
        {
            "path": "/ws/exposure/{account_id}",
            "protocol": "websocket",
            "description": (
                "Snapshot (ExposureSnapshot) сразу после подключения, затем "
                "ExposureUpdate после каждого исполненного ордера по счету."
            ),
            "onConnect": {"$ref": "#/components/schemas/ExposureSnapshot"},
            "onUpdate": {"$ref": "#/components/schemas/ExposureUpdate"},
        }
    ]

    # Attach a concrete 422 payload so Swagger UI shows the standard error shape
    # instead of just referencing HTTPValidationError.
    validation_error = schema["paths"]["/api/v1/orders"]["post"]["responses"].get("422")
    if validation_error is not None:
        validation_error["description"] = "Ошибка валидации тела запроса."
        validation_error.setdefault("content", {}).setdefault("application/json", {})[
            "example"
        ] = {
            "detail": [
                {
                    "type": "greater_than",
                    "loc": ["body", "quantity"],
                    "msg": "Input should be greater than 0",
                    "input": 0,
                    "ctx": {"gt": 0},
                }
            ]
        }

    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    uvicorn.run(
        app,
        host=os.getenv("MOCK_SERVER_HOST", "0.0.0.0"),
        port=int(os.getenv("MOCK_SERVER_PORT", "8000")),
        log_level="info",
    )
