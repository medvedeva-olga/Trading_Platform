"""FastAPI application: REST + WebSocket API for the mock trading platform.

Run locally::

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

All state lives in ``trading.db``; QA-only depth overrides are available when
``TRADING_QA_MODE=true``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .config import TICKERS, create_access_token, decode_access_token, money, settings
from .database import close_engine, init_db
from .market_feed import MarketFeed, Quote
from .schemas import (
    BalanceUpdatedEvent,
    BusinessError,
    CopyHistoryItem,
    CopySubscribeRequest,
    CopySubscriptionUpdate,
    CopyTradeResultEvent,
    DepositRequest,
    DepositResponse,
    DepthOverride,
    DepthResponse,
    ErrorResponse,
    LoginRequest,
    MasterStats,
    OrderCreateRequest,
    OrderResponse,
    OrderStatusEvent,
    PortfolioSummary,
    PositionClosedEvent,
    RegisterRequest,
    SubscriptionResponse,
    TickerQuote,
    TickEvent,
    TokenResponse,
    UserResponse,
)
from .trading_engine import TradingEngine
from .ws_manager import ConnectionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create schema, wire components, start the feed; clean up on shutdown."""
    await init_db()

    feed = MarketFeed()
    ws = ConnectionManager()
    engine = TradingEngine(feed, ws)

    async def publish_market_tick(ticker: str, quote: Quote) -> None:
        """Convert a feed quote into the public WS event and fan it out."""
        payload = TickEvent(
            ticker=ticker,
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            timestamp=quote.timestamp,
        ).model_dump(mode="json")
        ws.broadcast_tick(ticker, payload)

    # Registration order matters: push to clients first, then let the engine
    # evaluate GTC orders / SL-TP levels against the same tick.
    feed.subscribe(publish_market_tick)
    feed.subscribe(engine.handle_tick)

    app.state.feed = feed
    app.state.ws = ws
    app.state.engine = engine

    feed.start()
    logger.info("Trading platform ready (db=%s)", settings.database_url)
    try:
        yield
    finally:
        logger.info("Shutting down trading platform")
        await engine.shutdown()
        await feed.stop()
        await close_engine()


APP_DESCRIPTION = """
Реалистичный mock-стенд торговой платформы с копитрейдингом для QA Automation.

## Поток работы теста (изоляция через бизнес-логику)

1. `POST /api/v1/auth/register` — создайте уникального `MASTER` и `SLAVE`.
2. `POST /api/v1/auth/login` — получите JWT (HS256).
3. `POST /api/v1/portfolio/deposit` — пополните баланс (100 … 1 000 000 USDT).
4. `GET /api/v1/market/tickers` — возьмите текущие Bid/Ask и постройте ценовой коридор.
5. `GET /api/v1/market/depth?ticker=BTCUSDT` — посмотрите лучшие уровни стакана.
6. `POST /api/v1/orders` — выставьте `LIMIT` (лимит по коридору) или `MARKET`
   (исполнение по лучшей цене стакана), с `IOC` или `GTC`.
7. `POST /api/v1/copy-trading/subscribe` — подпишите `SLAVE` на `MASTER`.
8. `GET /api/v1/copy-trading/history` — проверьте `EXECUTED` / `SKIPPED` и причину.

## Стакан и исполнение

* Ликвидность — синтетический стакан вокруг текущей котировки плюс активные
  `NEW` / `PARTIALLY_FILLED` ордера. Приоритет — лучшая цена, затем время.
* Частичное исполнение переводит ордер в `PARTIALLY_FILLED`; остаток `GTC`
  остаётся в стакане с заморозкой, остаток `IOC` отменяется с причиной
  `IOC_PARTIALLY_FILLED` / `IOC_OUT_OF_RANGE`.
* Taker платит комиссию `fee_rate` (по умолчанию 0.1%). Исполнение дешевле лимита
  BUY (или дороже лимита SELL) фиксируется в `price_improvement`.
* Событие `order_status` дополнительно содержит `filled_qty`, `fee_amount`,
  `price_improvement`; каждое совпадение записывается в таблицу `trades`.
* Copy-trading запускается только при полном исполнении (`FILLED`) ордера Мастера.
* При `TRADING_QA_MODE=true` доступны скрытые ручки `POST /api/v1/test/depth`
  и `DELETE /api/v1/test/depth` для детерминированного стакана в тестах.

## WebSocket

* `/ws/market` — публичные тики. Клиент шлёт
  `{"action": "subscribe", "tickers": ["BTCUSDT"]}` (или `unsubscribe` / `set`).
* `/ws/user?token=<JWT>` — приватные события `order_status`, `position_closed`,
  `copy_trade_result`, `balance_updated`. Невалидный JWT → закрытие с кодом `4401`.

## Деньги

Все суммы — `Decimal` с точностью 8 знаков и сериализуются в JSON строками
(например, `"65000.00000000"`), чтобы исключить ошибки `float`.

## Переменные окружения (префикс `TRADING_`)

`TRADING_DATABASE_URL`, `TRADING_JWT_SECRET`, `TRADING_MARKET_REGIME`
(`calm`/`normal`/`high`), `TRADING_MARKET_VOLATILITY`, `TRADING_MARKET_SEED`,
`TRADING_SPREAD_BPS`, `TRADING_COPY_DELAY_MS` (мс задержки копирования;
установите больше тика, чтобы детерминированно тестировать `SLIPPAGE_EXCEEDED`),
`TRADING_FEE_RATE` (комиссия тейкера), `TRADING_SYNTHETIC_DEPTH_QTY`,
`TRADING_DEPTH_LEVELS`, `TRADING_QA_MODE`.
"""

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=APP_DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "auth", "description": "Регистрация и выдача JWT."},
        {"name": "portfolio", "description": "Депозиты, балансы, позиции и PnL."},
        {"name": "market", "description": "Котировки четырёх тикеров и стакан."},
        {"name": "orders", "description": "Создание, отмена и история ордеров."},
        {
            "name": "copy-trading",
            "description": "Мастера, подписки и история копирования.",
        },
        {"name": "system", "description": "Проверки живучести стенда."},
        {
            "name": "websocket",
            "description": "Потоковые события (документация в x-websockets).",
        },
    ],
)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
@app.exception_handler(BusinessError)
async def handle_business_error(_: Request, exc: BusinessError) -> JSONResponse:
    """Render domain errors as a stable, machine-readable envelope."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="JWT токен из /api/v1/auth/login (Authorization: Bearer <token>)",
)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    """Validate the JWT and expose a compact identity to endpoint handlers."""
    auth_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise auth_error
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = int(payload["sub"])
    except (ValueError, KeyError, TypeError):
        raise auth_error
    return {
        "user_id": user_id,
        "username": payload.get("username", ""),
        "role": payload.get("role", ""),
    }


CurrentUser = Annotated[dict[str, Any], Depends(get_current_user)]


def require_master(user: CurrentUser) -> dict[str, Any]:
    if user["role"] != "MASTER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="MASTER role required"
        )
    return user


def require_slave(user: CurrentUser) -> dict[str, Any]:
    if user["role"] != "SLAVE":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="SLAVE role required"
        )
    return user


MasterUser = Annotated[dict[str, Any], Depends(require_master)]
SlaveUser = Annotated[dict[str, Any], Depends(require_slave)]


def get_engine(request: Request) -> TradingEngine:
    return request.app.state.engine


EngineDep = Annotated[TradingEngine, Depends(get_engine)]


def get_feed(request: Request) -> MarketFeed:
    return request.app.state.feed


FeedDep = Annotated[MarketFeed, Depends(get_feed)]


# Reusable OpenAPI error fragment.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": "Нарушение бизнес-правила (нехватка средств, неверный диапазон и т.д.).",
    },
    401: {
        "model": ErrorResponse,
        "description": "Отсутствует, невалиден или истёк JWT.",
    },
    403: {
        "model": ErrorResponse,
        "description": "Недостаточно прав (роль) или чужой ресурс.",
    },
    404: {"model": ErrorResponse, "description": "Запрошенный ресурс не найден."},
    409: {
        "model": ErrorResponse,
        "description": "Конфликт состояния: активная подписка уже существует или больше не активна.",
    },
    422: {
        "model": ErrorResponse,
        "description": "Ошибка валидации тела/параметров запроса.",
    },
}


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: ERROR_RESPONSES[code] for code in codes}


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
@app.get("/", tags=["system"], summary="Корневой ответ", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": settings.app_name, "docs": "/docs", "version": "1.0.0"}


@app.get("/health", tags=["system"], summary="Проверка живучести")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def require_qa_mode() -> None:
    """Hide test-only endpoints unless the SUT is explicitly started in QA mode."""
    if not settings.qa_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@app.post(
    "/api/v1/test/depth",
    response_model=DepthResponse,
    tags=["system"],
    summary="QA: задать детерминированный стакан",
    include_in_schema=False,
)
async def set_depth(
    payload: DepthOverride,
    feed: FeedDep,
    _: None = Depends(require_qa_mode),
) -> DepthResponse:
    feed.set_depth(
        payload.ticker,
        [(money(price), money(qty)) for price, qty in payload.bids],
        [(money(price), money(qty)) for price, qty in payload.asks],
    )
    bids, asks = feed.get_depth(payload.ticker)
    return DepthResponse(
        ticker=payload.ticker,
        bids=bids,
        asks=asks,
        timestamp=feed.get_quote(payload.ticker).timestamp,
    )


@app.delete(
    "/api/v1/test/depth",
    tags=["system"],
    summary="QA: вернуть синтетический стакан",
    include_in_schema=False,
)
async def reset_depth(
    feed: FeedDep,
    ticker: str | None = Query(default=None),
    _: None = Depends(require_qa_mode),
) -> dict[str, str]:
    if ticker is not None and ticker not in TICKERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid ticker"
        )
    feed.reset_depth(ticker)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post(
    "/api/v1/auth/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
    summary="Зарегистрировать пользователя (MASTER или SLAVE)",
    description="Каждый тест может создать изолированного пользователя с уникальным `username`.",
    responses=errors(400, 422),
)
async def register(payload: RegisterRequest, engine: EngineDep) -> UserResponse:
    return await engine.register(payload.username, payload.password, payload.role)


@app.post(
    "/api/v1/auth/login",
    response_model=TokenResponse,
    tags=["auth"],
    summary="Получить JWT по логину и паролю",
    responses=errors(401, 422),
)
async def login(payload: LoginRequest, engine: EngineDep) -> TokenResponse:
    user = await engine.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return TokenResponse(
        access_token=create_access_token(user.id, user.username, user.role),
        token_type="bearer",
        expires_in=settings.access_token_ttl_seconds,
        user_id=user.id,
        role=user.role,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
@app.post(
    "/api/v1/portfolio/deposit",
    response_model=DepositResponse,
    tags=["portfolio"],
    summary="Пополнить виртуальный баланс USDT",
    description="Сумма от 100 до 1 000 000 USDT; операция пишется в историю транзакций.",
    responses=errors(400, 401, 422),
)
async def deposit(
    payload: DepositRequest, user: CurrentUser, engine: EngineDep
) -> DepositResponse:
    return await engine.deposit(user["user_id"], payload.amount)


@app.get(
    "/api/v1/portfolio/summary",
    response_model=PortfolioSummary,
    tags=["portfolio"],
    summary="Свободный/замороженный баланс, позиции и PnL",
    responses=errors(401, 404),
)
async def portfolio_summary(user: CurrentUser, engine: EngineDep) -> PortfolioSummary:
    return await engine.get_summary(user["user_id"])


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------
@app.get(
    "/api/v1/market/tickers",
    response_model=list[TickerQuote],
    tags=["market"],
    summary="Текущие котировки (bid / ask / last / spread)",
    description=(
        "Котировки обновляются каждые "
        f"{settings.market_tick_interval_ms} мс. Используйте их для расчёта динамического ценового коридора."
    ),
    responses=errors(422),
)
async def tickers(feed: FeedDep) -> list[TickerQuote]:
    quotes = feed.get_quotes()
    return [
        TickerQuote(
            ticker=ticker,
            bid=quote.bid,
            ask=quote.ask,
            last=quote.last,
            spread=quote.spread,
            timestamp=quote.timestamp,
        )
        for ticker, quote in quotes.items()
    ]


@app.get(
    "/api/v1/market/depth",
    response_model=DepthResponse,
    tags=["market"],
    summary="Стакан: лучшие bid/ask с объёмами",
    description=(
        "Объединяет синтетическую ликвидность и resting-ордера. "
        "Параметр `limit` ограничивает число уровней с каждой стороны."
    ),
    responses=errors(400),
)
async def depth(
    feed: FeedDep,
    ticker: str = Query(default="BTCUSDT"),
    limit: int = Query(default=5, ge=1, le=20),
) -> DepthResponse:
    if ticker not in TICKERS:
        raise BusinessError("INVALID_TICKER", f"Unsupported ticker: {ticker}")
    bids, asks = feed.get_depth(ticker)
    return DepthResponse(
        ticker=ticker,
        bids=bids[:limit],
        asks=asks[:limit],
        timestamp=feed.get_quote(ticker).timestamp,
    )


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@app.post(
    "/api/v1/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["orders"],
    summary="Создать ордер (LIMIT/MARKET, IOC/GTC)",
    description=(
        "`LIMIT` исполняется только в коридоре `min_price..max_price`, `MARKET` — по лучшей "
        "цене стакана. Частичное исполнение даёт `PARTIALLY_FILLED`; остаток `IOC` отменяется "
        "с `reason=IOC_PARTIALLY_FILLED` / `IOC_OUT_OF_RANGE`, остаток `GTC` остаётся в стакане "
        "с заморозкой в `frozen_balance`. Тейкер платит комиссию `fee_rate`; разница с лимитом "
        "фиксируется в `price_improvement`."
    ),
    responses=errors(400, 401, 403, 404, 422),
)
async def create_order(
    payload: OrderCreateRequest, user: CurrentUser, engine: EngineDep
) -> OrderResponse:
    return await engine.create_order(user["user_id"], payload)


@app.get(
    "/api/v1/orders",
    response_model=list[OrderResponse],
    tags=["orders"],
    summary="История ордеров текущего пользователя",
    responses=errors(401, 422),
)
async def list_orders(
    user: CurrentUser,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[OrderResponse]:
    return await engine.list_orders(user["user_id"], limit)


@app.get(
    "/api/v1/orders/{order_id}",
    response_model=OrderResponse,
    tags=["orders"],
    summary="Получить ордер по ID",
    responses=errors(401, 403, 404, 422),
)
async def get_order(
    order_id: int, user: CurrentUser, engine: EngineDep
) -> OrderResponse:
    order = await engine.get_order(user["user_id"], order_id)
    return OrderResponse.model_validate(order)


@app.delete(
    "/api/v1/orders/{order_id}",
    response_model=OrderResponse,
    tags=["orders"],
    summary="Отменить отложенный GTC-ордер и разморозить средства",
    responses=errors(400, 401, 403, 404, 422),
)
async def cancel_order(
    order_id: int, user: CurrentUser, engine: EngineDep
) -> OrderResponse:
    return await engine.cancel_order(user["user_id"], order_id)


# ---------------------------------------------------------------------------
# Copy trading
# ---------------------------------------------------------------------------
@app.get(
    "/api/v1/copy-trading/masters",
    response_model=list[MasterStats],
    tags=["copy-trading"],
    summary="Список Master-трейдеров со статистикой",
    responses=errors(401, 422),
)
async def masters(_: CurrentUser, engine: EngineDep) -> list[MasterStats]:
    return await engine.get_masters()


@app.post(
    "/api/v1/copy-trading/subscribe",
    response_model=SubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["copy-trading"],
    summary="Подписать SLAVE на MASTER",
    description=(
        "`PROPORTIONAL` — доля `allocation_usdt / equity мастера`; "
        "`FIXED_RATIO` — фиксированный коэффициент `fixed_ratio` от объёма мастера. "
        "Дублирование активной подписки запрещено (409 Conflict). "
        "Для изменения параметров используйте PATCH /subscriptions/{id}. "
        "После отписки создаётся новая запись с новым id — старая сохраняется для аудита."
    ),
    responses=errors(400, 401, 403, 404, 409, 422),
)
async def subscribe(
    payload: CopySubscribeRequest, user: SlaveUser, engine: EngineDep
) -> SubscriptionResponse:
    return await engine.subscribe(user["user_id"], payload)


@app.get(
    "/api/v1/copy-trading/subscriptions",
    response_model=list[SubscriptionResponse],
    tags=["copy-trading"],
    summary="Подписки текущего SLAVE",
    responses=errors(401, 422),
)
async def subscriptions(
    user: SlaveUser, engine: EngineDep
) -> list[SubscriptionResponse]:
    return await engine.list_subscriptions(user["user_id"])


@app.patch(
    "/api/v1/copy-trading/subscriptions/{subscription_id}",
    response_model=SubscriptionResponse,
    tags=["copy-trading"],
    summary="Изменить параметры активной подписки",
    description=(
        "Обновляет параметры существующей подписки, сохраняя её id и created_at. "
        "Смену мастера выполняйте через POST /subscribe: это создаст новую запись."
    ),
    responses=errors(400, 401, 403, 404, 409, 422),
)
async def update_subscription(
    subscription_id: int,
    payload: CopySubscriptionUpdate,
    user: SlaveUser,
    engine: EngineDep,
) -> SubscriptionResponse:
    return await engine.update_subscription(user["user_id"], subscription_id, payload)


@app.delete(
    "/api/v1/copy-trading/subscriptions/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["copy-trading"],
    summary="Отписаться от MASTER",
    responses=errors(401, 403, 404, 422),
)
async def unsubscribe(subscription_id: int, user: SlaveUser, engine: EngineDep) -> None:
    await engine.unsubscribe(user["user_id"], subscription_id)


@app.get(
    "/api/v1/copy-trading/history",
    response_model=list[CopyHistoryItem],
    tags=["copy-trading"],
    summary="Лог попыток копирования (EXECUTED / SKIPPED)",
    description="Для `SKIPPED` поле `skip_reason` содержит `SLIPPAGE_EXCEEDED` или `INSUFFICIENT_FUNDS`.",
    responses=errors(401, 422),
)
async def copy_history(
    user: CurrentUser,
    engine: EngineDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CopyHistoryItem]:
    return await engine.get_copy_history(user["user_id"], limit)


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------
class WSChannelMessage(BaseModel):
    """Клиентское сообщение для управления подписками `/ws/market`."""

    action: Literal["subscribe", "unsubscribe", "set"] = Field(
        description="`subscribe`/`unsubscribe` добавляют/убирают тикеры, `set` заменяет список."
    )
    tickers: list[str] = Field(
        default_factory=list,
        description="Подмножество BTCUSDT, ETHUSDT, SOLUSDT, TONUSDT.",
    )


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket) -> None:
    """Public tick stream. Clients send subscribe/unsubscribe/set actions."""
    await websocket.accept()
    manager: ConnectionManager = websocket.app.state.ws
    connection = manager.attach_market(websocket)
    # Sensible default: stream all instruments until the client narrows it.
    manager.set_tickers(connection, list(TICKERS))
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = WSChannelMessage.model_validate_json(raw)
            except Exception:
                connection.enqueue(
                    {
                        "event": "error",
                        "detail": "Expected JSON with action and tickers",
                    }
                )
                continue

            unknown = [t for t in message.tickers if t not in TICKERS]
            if unknown:
                connection.enqueue(
                    {"event": "error", "detail": f"Unknown tickers: {unknown}"}
                )
                continue

            if message.action == "subscribe":
                manager.add_tickers(connection, message.tickers)
            elif message.action == "unsubscribe":
                manager.remove_tickers(connection, message.tickers)
            else:
                manager.set_tickers(connection, message.tickers)
            connection.enqueue(
                {
                    "event": "subscription",
                    "action": message.action,
                    "tickers": message.tickers,
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        manager.detach_market(connection)


@app.websocket("/ws/user")
async def ws_user(
    websocket: WebSocket,
    token: Annotated[str, Query(description="JWT из /api/v1/auth/login")],
) -> None:
    """Private event stream; JWT is verified before the socket is accepted."""
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except (ValueError, KeyError, TypeError):
        # Accept first so the client receives a stable application close code
        # instead of a bare HTTP 403 handshake rejection.
        await websocket.accept()
        await websocket.close(code=4401, reason="Invalid or expired JWT")
        return

    await websocket.accept()
    manager: ConnectionManager = websocket.app.state.ws
    connection = manager.attach_user(user_id, websocket)
    connection.enqueue(
        {
            "event": "connected",
            "user_id": user_id,
            "detail": "Private stream established",
        }
    )
    try:
        while True:
            # Drain inbound frames (keepalive / pings); all real events are pushed.
            raw = await websocket.receive_text()
            if raw.strip() == "ping":
                connection.enqueue({"event": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        manager.detach_user(user_id, connection)


# ---------------------------------------------------------------------------
# OpenAPI enrichment: document WebSocket channels and event schemas
# ---------------------------------------------------------------------------
_original_openapi = app.openapi


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = _original_openapi()
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    for model in (
        TickEvent,
        OrderStatusEvent,
        PositionClosedEvent,
        CopyTradeResultEvent,
        BalanceUpdatedEvent,
        WSChannelMessage,
    ):
        schemas[model.__name__] = model.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )

    schema["x-websockets"] = {
        "/ws/market": {
            "description": "Публичный поток тиков. Клиент отправляет `subscribe`/`unsubscribe`/`set`.",
            "client_message_schema": "#/components/schemas/WSChannelMessage",
            "server_events": ["#/components/schemas/TickEvent"],
        },
        "/ws/user": {
            "description": "Приватный поток: `?token=<JWT>`. Невалидный токен → close(4401).",
            "server_events": [
                "#/components/schemas/OrderStatusEvent",
                "#/components/schemas/PositionClosedEvent",
                "#/components/schemas/CopyTradeResultEvent",
                "#/components/schemas/BalanceUpdatedEvent",
            ],
        },
    }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi  # type: ignore[method-assign]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="info",
    )
