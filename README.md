# Copy Trading Mock Exchange — SUT для QA Automation

Реалистичный mock-стенд трейдинговой платформы (System Under Test) для отработки
API- и WebSocket-автотестов на **pytest + httpx + websockets**.

Стенд намеренно ведёт себя как небольшой продакшен-сервис:

* состояние хранится в файле **`trading.db`** (SQLite) и сохраняется между перезапусками;
* все деньги — `Decimal` с точностью 8 знаков и сериализуются строками (`"65000.00000000"`);
* регистрация, логин, депозит и транзакции — реальная бизнес-логика;
* ордера исполняются через стакан: `LIMIT` / `MARKET`, `IOC` / `GTC`, частичные
  исполнения, комиссия тейкера и price improvement;
* экспозиция счета доступна через REST и первое WebSocket-сообщение, а каждое
  исполнение присылает дельту экспозиции;
* **нет** искусственных ручек сброса БД / «_set price_» — изоляция тестов достигается
  уникальными пользователями и пополнением баланса. Исключение: QA-оверрайд стакана
  при `TRADING_QA_MODE=true` (см. ниже).

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
| Исполнение | стакан, best price/time priority, частичные fills, `trades` |

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
poetry run pytest tests/orders/test_order_book.py -v

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
до импорта приложения (включая `TRADING_QA_MODE=true`). Маркеры: `smoke`,
`copy`, `websocket`.

### Статус покрытия и планы

Устойчиво покрыты домены `auth`, `copy_trading`, `websocket`, `orders` и
`portfolio` (экспозиция). Order-тесты проверяют полный цикл `IOC` / `GTC`,
отмену с разморозкой, `stop_loss` / `take_profit`, стакан, `MARKET` / `LIMIT`,
частичное исполнение, комиссии и price improvement. Экспозиционные тесты
проверяют REST/WS-снимок и дельты при покупке и продаже. Всего в наборе 49
тестов, прогон зелёный.
Тесты используют общего клиента, фабрики данных и доменные `StrEnum`-константы
из `src/constants.py`.

Планируемое расширение текущих доменов:

* **auth** — негативные кейсы JWT (истёкший и повреждённый токен), валидация
  регистрационных данных (`422`), проверка прав доступа к приватным ручкам;
* **copy_trading** — граничные значения `PATCH` (пустой body, невалидное
  сочетание `copy_mode` / `fixed_ratio`), несколько Slave на одного Master,
  повторные подписки и отписки, больше сценариев `SKIPPED`;
* **websocket** — явные тесты действий `unsubscribe` и `set`, негативные кадры
  подписки, переподключения и коды закрытия, порядок событий;
* **orders** — матчинг двух пользовательских ордеров (maker/maker), сценарии
  `SELL` против стакана, auditing таблицы `trades`, отмена `PARTIALLY_FILLED`
  с частичной разморозкой;
* **контракты** — сверка QA-зеркал из `src/schemas/` с OpenAPI приложения,
  чтобы вовремя находить расхождение схем.

Отдельно планируется добавить в тестовый проект другие домены SUT, которые
сейчас не покрыты:

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
| `TRADING_FEE_RATE` | `0.001` | Комиссия тейкера (0.1%) |
| `TRADING_SYNTHETIC_DEPTH_QTY` | `1000` | Объём каждого синтетического уровня |
| `TRADING_DEPTH_LEVELS` | `5` | Число уровней с каждой стороны |
| `TRADING_QA_MODE` | `false` | Включить скрытые QA-ручки стакана |
| `TRADING_MIN_DEPOSIT` / `TRADING_MAX_DEPOSIT` | `100` / `1000000` | Лимиты депозита |
| `TRADING_HOST` / `TRADING_PORT` | `0.0.0.0` / `8000` | Для `python -m app.main` |

### Детерминированный тест проскальзывания

Установите `TRADING_COPY_DELAY_MS=1500` (больше интервала тика) и
`max_slippage_percent` около `0.01`. Тогда копирующий ордер почти наверняка
попадёт в `SKIPPED` с причиной `SLIPPAGE_EXCEEDED`.

```bash
TRADING_COPY_DELAY_MS=1500 TRADING_MARKET_REGIME=high uvicorn app.main:app --reload
```

### Детерминированный стакан в тестах

`tests/conftest.py` выставляет `TRADING_QA_MODE=true`, поэтому в прогоне доступны
две скрытые ручки (в OpenAPI они не показываются):

```bash
# задать стакан: [[цена, объём], ...]
curl -s -X POST http://127.0.0.1:8000/api/v1/test/depth \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"BTCUSDT","bids":[],"asks":[["65000.00000000","0.05000000"]]}'

# вернуть синтетический стакан (все тикеры или ?ticker=BTCUSDT)
curl -s -X DELETE http://127.0.0.1:8000/api/v1/test/depth
```

Без `TRADING_QA_MODE=true` эти ручки отвечают `404`. Оверрайд живёт в памяти
процесса и сбрасывается autouse-фикстурой в `tests/orders/test_order_book.py`,
поэтому не влияет на другие сценарии.

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

# 2. Текущие котировки и стакан
curl -s http://127.0.0.1:8000/api/v1/market/tickers -H "Authorization: Bearer $TOKEN"
curl -s 'http://127.0.0.1:8000/api/v1/market/depth?ticker=BTCUSDT&limit=5' \
  -H "Authorization: Bearer $TOKEN"

# 3. IOC-ордер в коридоре текущей цены
curl -s -X POST http://127.0.0.1:8000/api/v1/orders \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "ticker":"BTCUSDT","side":"BUY","order_type":"LIMIT","amount":"0.10000000",
    "min_price":"60000.00000000","max_price":"70000.00000000",
    "time_in_force":"IOC","stop_loss":null,"take_profit":null
  }'

# 4. Slave + подписка
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"qa_slave_001","password":"StrongPass123!","role":"SLAVE"}'
```

`order_type` — `LIMIT` (по умолчанию) или `MARKET`.

---

## REST API

| Метод | Путь | Описание |
|---|---|---|
| `POST` | `/api/v1/auth/register` | Регистрация (`MASTER` / `SLAVE`) |
| `POST` | `/api/v1/auth/login` | Выдача JWT |
| `POST` | `/api/v1/portfolio/deposit` | Пополнение баланса (запись в транзакции) |
| `GET` | `/api/v1/portfolio/summary` | Баланс, позиции, market value, PnL |
| `GET` | `/api/v1/portfolio/exposure` | Знаковая экспозиция по открытым позициям |
| `GET` | `/api/v1/market/tickers` | Bid / Ask / Last / Spread |
| `GET` | `/api/v1/market/depth` | Стакан: `bids` / `asks` с объёмами |
| `POST` | `/api/v1/orders` | Ордер (`LIMIT` с `IOC`/`GTC`, `MARKET` только с `IOC`) |
| `GET` | `/api/v1/orders` | История ордеров пользователя |
| `GET` | `/api/v1/orders/{id}` | Ордер по ID |
| `DELETE` | `/api/v1/orders/{id}` | Отмена `NEW` / `PARTIALLY_FILLED` + разморозка |
| `GET` | `/api/v1/copy-trading/masters` | Мастера и статистика |
| `POST` | `/api/v1/copy-trading/subscribe` | Подписка Slave на Master |
| `GET` | `/api/v1/copy-trading/subscriptions` | Подписки Slave |
| `PATCH` | `/api/v1/copy-trading/subscriptions/{id}` | Изменение параметров подписки |
| `DELETE` | `/api/v1/copy-trading/subscriptions/{id}` | Отписка |
| `GET` | `/api/v1/copy-trading/history` | Лог копирования |

QA-ручки стакана (`POST` / `DELETE /api/v1/test/depth`) доступны только при
`TRADING_QA_MODE=true` и исключены из OpenAPI.

### Стакан и исполнение ордеров

* Источник ликвидности — синтетический стакан вокруг текущей котировки и активные
  ордера других пользователей. Приоритет: лучшая цена, затем время.
* Статусы: `NEW` (resting до исполнения), `PARTIALLY_FILLED`, `FILLED`, `CANCELED`.
  `PENDING` сохранён как устаревший псевдоним в схемах.
* `IOC` без полного исполнения → `CANCELED` с `reason=IOC_PARTIALLY_FILLED` или
  `IOC_OUT_OF_RANGE`; остаток `GTC` остаётся в стакане с заморозкой средств.
* `GTC` применяется только к `LIMIT`: сочетание `MARKET + GTC` отклоняется с
  `400 MARKET_GTC_NOT_ALLOWED`, поскольку рыночный ордер не может «лежать» в стакане.
* Комиссия `fee_rate` (0.1%) списывается с тейкера; исполнение по цене мейкера,
  лучшей чем лимит, отражается в `price_improvement`. Каждое совпадение пишется
  в таблицу `trades`.
* Copy-trading запускается только при `FILLED` ордера Мастера;
  `PARTIALLY_FILLED` копирование не запускает.

### Экспозиция

`GET /api/v1/portfolio/exposure` возвращает `total_exposure = sum(quantity × mark_price)`
по открытым long-позициям; `mark_price` — текущий bid. Комиссии и замороженные
средства в экспозицию не входят. Это рисковая метрика, а не остаток денег:
увеличение экспозиции при `BUY` и уменьшение при `SELL` приходят в `/ws/user`
событием `exposure_updated` (`delta = ±quantity × price`, `last_order_id`).
Первое сообщение после подключения — `exposure_snapshot`, идентичное REST-ответу
на момент подключения. Значения — Decimal-строки; шорты SUT не поддерживает,
поэтому экспозиция не бывает отрицательной.

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
`CANNOT_COPY_SELF`, `SUBSCRIPTION_ALREADY_EXISTS`, `MASTER_ID_IMMUTABLE`,
`MARKET_GTC_NOT_ALLOWED`, `FORBIDDEN`.
Auth/валидация — стандартные `401` / `422` с полем `detail`.

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

* `exposure_snapshot` — первое сообщение после подключения, совпадает с REST
  `/api/v1/portfolio/exposure` на момент подключения;
* `exposure_updated` — дельта экспозиции после исполнения (`delta`, `last_order_id`);
* `order_status` — `NEW` / `PARTIALLY_FILLED` / `FILLED` / `CANCELED`, плюс
  `filled_qty`, `fee_amount`, `price_improvement` и `reason`;
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
5. Если тест меняет стакан через `/api/v1/test/depth`, сбрасывайте оверрайд в
   `finally` / `yield`-фикстуре (`DELETE /api/v1/test/depth`).

Никаких общих мутабельных состояний и сбросов между тестами нет — каждый тест
работает со своим пользователем и своим бюджетом.

---

## Структура файлов

```
app/               # код трейдинговой платформы (SUT)
  config.py        # настройки, константы, money(), JWT-хелперы
  database.py      # async engine, сессии, init_db()
  models.py        # SQLAlchemy ORM-таблицы (включая trades)
  schemas.py       # Pydantic v2 схемы, BusinessError, WS-события
  market_feed.py   # random-walk котировки, синтетический/QA-стакан
  trading_engine.py  # стакан, матчинг, комиссии, IOC/GTC, SL/TP, копитрейдинг
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
  auth/  copy_trading/  orders/  portfolio/  websocket/
  conftest.py      # Lifecycle SUT, изолированные пользователи, QA mode
  config.py        # настройки тестов с префиксом QA_
```

## Таблицы в `trading.db`

`users`, `portfolios`, `transactions`, `positions`, `orders`, `trades`,
`copy_subscriptions`, `copy_trade_history`. Все денежные колонки —
`NUMERIC(20, 8)`; включены `PRAGMA foreign_keys=ON` и `journal_mode=WAL`.
