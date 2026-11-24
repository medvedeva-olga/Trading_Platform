# Copy Trading Mock Exchange — SUT для QA Automation

Реалистичный mock-стенд трейдинговой платформы (System Under Test) для отработки
API- и WebSocket-автотестов на **pytest + httpx + websockets**.

Стенд намеренно ведёт себя как небольшой продакшен-сервис:

* состояние хранится в файле **`trading.db`** (SQLite) и сохраняется между перезапусками;
* все деньги — `Decimal` с точностью 8 знаков и сериализуются строками (`"65000.00000000"`);
* регистрация, логин, депозит и транзакции — реальная бизнес-логика;
* **нет** искусственных ручек сброса БД / «_set price_» — изоляция тестов достигается
  уникальными пользователями и пополнением баланса.

---

## Стек

| Компонент | Технология |
|---|---|
| API | FastAPI (Pydantic v2) |
| Сервер | Uvicorn |
| БД | SQLite через SQLAlchemy 2.0 Async + aiosqlite |
| Файл БД | `trading.db` |
| Авторизация | JWT (HS256, PyJWT) + bcrypt |
| Потоки | WebSocket (`/ws/market`, `/ws/user`) |
| Деньги | `decimal.Decimal`, `NUMERIC(20, 8)` |

---

## Установка и запуск

Python 3.11+.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Для разработки:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Альтернативно (использует `TRADING_HOST` / `TRADING_PORT`):

```bash
python -m app.main
```

Документация:

* Swagger UI — <http://127.0.0.1:8000/docs>
* ReDoc — <http://127.0.0.1:8000/redoc>
* OpenAPI JSON — <http://127.0.0.1:8000/openapi.json> (секция `x-websockets` описывает WS-события)

---

## Запуск тестов

Готовый набор автотестов лежит в `tests/` (по доменам) и `src/` (клиент,
фабрики данных, ожидания). Тесты поднимают **реальный uvicorn-сервер** в фоновом
потоке на свободном порту и работают с отдельной БД `tests_trading.db`, не
затрагивая `trading.db`.

```bash
# Все тесты
poetry run pytest -v

# Только критический путь
poetry run pytest -m smoke -v

# Конкретный домен / файл
poetry run pytest tests/copy_trading/test_copy_trading.py -v

# Линтеры и форматирование (включая код приложения)
poetry run black --check app/ src/ tests/
poetry run isort --check-only app/ src/ tests/
poetry run flake8 app/ src/ tests/
```

Если Poetry не установлен, те же команды работают через `python -m`:
`python -m pytest -v`, `python -m black ...` и т.д. На Windows с не-UTF-8
локалью перед `isort` задайте `set PYTHONUTF8=1`, иначе isort пропустит
`app/main.py` с кириллицей в строках документации.

Настройки самих тестов (порт, таймауты, депозиты) читаются с префиксом `QA_`
из `tests/config.py`; переменные окружения SUT выставляет `tests/conftest.py`
до импорта приложения. Маркеры: `smoke`, `copy`, `websocket`.

### Статус покрытия и планы

Набор автотестов **ещё в процессе написания**. На данный момент устойчиво
покрыты три домена — `auth`, `copy_trading`, `websocket`. Тесты используют
общего клиента, фабрики данных и доменные `StrEnum`-константы из
`src/constants.py`.

Планируемое расширение текущих доменов:

* **auth** — негативные кейсы JWT (истёкший и повреждённый токен), валидация
  регистрационных данных (`422`), проверка прав доступа к приватным ручкам;
* **copy_trading** — граничные значения `PATCH` (пустой body, невалидное
  сочетание `copy_mode` / `fixed_ratio`), несколько Slave на одного Master,
  повторные подписки и отписки, больше сценариев `SKIPPED`;
* **websocket** — явные тесты действий `unsubscribe` и `set`, негативные кадры
  подписки, переподключения и коды закрытия, порядок событий;
* **контракты** — сверка QA-зеркал из `src/schemas/` с OpenAPI приложения,
  чтобы вовремя находить расхождение схем.

Отдельно планируется добавить в тестовый проект другие домены SUT, которые
сейчас не покрыты:

* **orders** — жизненный цикл `IOC` / `GTC`, отмена с разморозкой средств,
  срабатывание `stop_loss` / `take_profit`, ошибки `INSUFFICIENT_FUNDS`,
  `INVALID_TICKER`, `ORDER_NOT_FOUND`;
* **portfolio** — лимиты депозита, история транзакций, расчёт equity / PnL и
  замороженного баланса;
* **market** — схема котировок, соотношение `bid <= last <= ask`, спред,
  интервал тиков и режимы рынка;
* **integration** — сквозные сценарии: регистрация → депозит → ордер →
  подписка → история копирования → WebSocket-события.

---

## Конфигурация

Все переменные имеют префикс `TRADING_` (поддерживается файл `.env`).

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `TRADING_DATABASE_URL` | `sqlite+aiosqlite:///./trading.db` | Путь к БД |
| `TRADING_JWT_SECRET` | `change-me-in-production-qa-only-secret` | Секрет подписи JWT |
| `TRADING_ACCESS_TOKEN_TTL_SECONDS` | `3600` | Время жизни токена |
| `TRADING_MARKET_TICK_INTERVAL_MS` | `500` | Интервал котировок |
| `TRADING_MARKET_REGIME` | `normal` | `calm` / `normal` / `high` |
| `TRADING_MARKET_VOLATILITY` | — | Явная переопределённая волатильность (имеет приоритет) |
| `TRADING_MARKET_SEED` | — | Seed генератора случайных чисел |
| `TRADING_SPREAD_BPS` | `10` | Спред в базисных пунктах (1 bp = 0.01%) |
| `TRADING_COPY_DELAY_MS` | `250` | Задержка копирования сделки Мастера |
| `TRADING_MIN_DEPOSIT` / `TRADING_MAX_DEPOSIT` | `100` / `1000000` | Лимиты депозита |
| `TRADING_HOST` / `TRADING_PORT` | `0.0.0.0` / `8000` | Для `python -m app.main` |

### Детерминированный тест проскальзывания

Установите `TRADING_COPY_DELAY_MS=1500` (больше интервала тика) и
`max_slippage_percent` около `0.01`. Тогда копирующий ордер почти наверняка
попадёт в `SKIPPED` с причиной `SLIPPAGE_EXCEEDED`.

```bash
TRADING_COPY_DELAY_MS=1500 TRADING_MARKET_REGIME=high uvicorn app.main:app --reload
```

---

## Быстрый сценарий (curl)

```bash
# 1. Мастер
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"qa_master_001","password":"StrongPass123!","role":"MASTER"}'

curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"qa_master_001","password":"StrongPass123!"}'
# -> {"access_token":"eyJ...", ...}

export TOKEN=<access_token>

curl -s -X POST http://127.0.0.1:8000/api/v1/portfolio/deposit \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"amount":"10000.00000000"}'

# 2. Текущие котировки (построить коридор от bid/ask)
curl -s http://127.0.0.1:8000/api/v1/market/tickers -H "Authorization: Bearer $TOKEN"

# 3. IOC-ордер в коридоре текущей цены
curl -s -X POST http://127.0.0.1:8000/api/v1/orders \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "ticker":"BTCUSDT","side":"BUY","amount":"0.10000000",
    "min_price":"60000.00000000","max_price":"70000.00000000",
    "time_in_force":"IOC","stop_loss":null,"take_profit":null
  }'

# 4. Slave + подписка
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"qa_slave_001","password":"StrongPass123!","role":"SLAVE"}'
```

---

## REST API

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/v1/auth/register` | Регистрация (`MASTER` / `SLAVE`) |
| `POST` | `/api/v1/auth/login` | Выдача JWT |
| `POST` | `/api/v1/portfolio/deposit` | Пополнение баланса (запись в транзакции) |
| `GET` | `/api/v1/portfolio/summary` | Баланс, позиции, market value, PnL |
| `GET` | `/api/v1/market/tickers` | Bid / Ask / Last / Spread |
| `POST` | `/api/v1/orders` | Создание ордера (`IOC` / `GTC`) |
| `GET` | `/api/v1/orders` | История ордеров пользователя |
| `GET` | `/api/v1/orders/{id}` | Ордер по ID |
| `DELETE` | `/api/v1/orders/{id}` | Отмена GTC + разморозка средств |
| `GET` | `/api/v1/copy-trading/masters` | Мастера и статистика |
| `POST` | `/api/v1/copy-trading/subscribe` | Подписка Slave на Master |
| `GET` | `/api/v1/copy-trading/subscriptions` | Подписки Slave |
| `DELETE` | `/api/v1/copy-trading/subscriptions/{id}` | Отписка |
| `GET` | `/api/v1/copy-trading/history` | Лог копирования |

### Формат ошибок

Бизнес-ошибки возвращают стабильный envelope:

```json
{
  "detail": {
    "code": "INSUFFICIENT_FUNDS",
    "message": "Not enough free balance for this order.",
    "details": {"required": "6500.00000000", "available": "100.00000000"}
  }
}
```

Коды: `USERNAME_ALREADY_EXISTS`, `INVALID_DEPOSIT_AMOUNT`, `INSUFFICIENT_FUNDS`,
`INSUFFICIENT_POSITION`, `INVALID_TICKER`, `AMOUNT_TOO_SMALL`, `ORDER_NOT_FOUND`,
`ORDER_NOT_CANCELABLE`, `MASTER_NOT_FOUND`, `NOT_A_MASTER`, `ROLE_NOT_ALLOWED`,
`CANNOT_COPY_SELF`, `FORBIDDEN`. Auth/валидация — стандартные `401` / `422`
с полем `detail`.

---

## WebSocket

### `/ws/market` (публичный)

Клиент отправляет:

```json
{"action": "subscribe", "tickers": ["BTCUSDT"]}
```

Поддерживаемые `action`: `subscribe`, `unsubscribe`, `set`. По умолчанию поток
всех четырёх тикеров. Сервер присылает:

```json
{"event":"tick","ticker":"BTCUSDT","bid":"64996.75000000","ask":"65003.25000000","last":"65000.00000000","timestamp":"2026-11-19T10:00:00.000Z"}
```

### `/ws/user?token=<JWT>` (приватный)

Токен проверяется **до** установки соединения; невалидный → `close(4401)`.

События:

* `order_status` — `PENDING` / `FILLED` / `CANCELED`;
* `position_closed` — с итоговым `pnl` и `reason` (`MANUAL` / `STOP_LOSS` / `TAKE_PROFIT`);
* `copy_trade_result` — `EXECUTED` или `SKIPPED` с `reason`;
* `balance_updated` — `cash_balance` / `frozen_balance`.

Пример на `websockets`:

```python
import asyncio, json, websockets

async def main():
    uri = "ws://127.0.0.1:8000/ws/user?token=" + JWT
    async with websockets.connect(uri) as ws:
        async for raw in ws:
            event = json.loads(raw)
            print(event["event"], event)

asyncio.run(main())
```

---

## Как добиться изоляции тестов

1. Генерируйте уникальный `username` на тест (например, `f"qa_{uuid4().hex[:12]}"`).
2. Регистрируйте пользователя через `POST /api/v1/auth/register`.
3. Пополняйте баланс через `POST /api/v1/portfolio/deposit`.
4. Создавайте ордера/подписки от имени этого пользователя и проверяйте результат
   через REST и прямую проверку `trading.db` при необходимости.

Никаких общих мутаibles состояний и сбросов между тестами нет — каждый тест
работает со своим пользователем и своим бюджетом.

---

## Структура файлов

```
app/               # код трейдинговой платформы (SUT)
  config.py        # настройки, константы, money(), JWT-хелперы
  database.py      # async engine, сессии, init_db()
  models.py        # SQLAlchemy ORM-таблицы
  schemas.py       # Pydantic v2 схемы, BusinessError, WS-события
  market_feed.py   # random-walk котировки каждые 500 мс
  trading_engine.py  # балансы, ордера, GTC, SL/TP, копитрейдинг
  ws_manager.py    # менеджер WS-подключений и FIFO-очередей
  main.py          # приложение, REST, WebSocket, OpenAPI
requirements.txt
README.md
pyproject.toml     # black / isort / pytest (маркеры, asyncio_mode)
.flake8
src/               # общая QA-инфраструктура
  clients/         # httpx/websockets-обёртка + валидация ответов
  fixtures/        # фабрики данных и поллинг без time.sleep()
  schemas/         # Pydantic-контракты (auth, copy-trading, websocket, order, market)
tests/             # тесты по доменам
  auth/  copy_trading/  websocket/
  conftest.py      # Lifecycle SUT, изолированные пользователи
  config.py        # настройки тестов с префиксом QA_
```

## Таблицы в `trading.db`

`users`, `portfolios`, `transactions`, `positions`, `orders`,
`copy_subscriptions`, `copy_trade_history`. Все денежные колонки —
`NUMERIC(20, 8)`; включены `PRAGMA foreign_keys=ON` и `journal_mode=WAL`.
