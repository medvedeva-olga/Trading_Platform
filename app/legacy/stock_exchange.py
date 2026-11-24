import asyncio
import random
import time
import uuid
from typing import Dict, List, Optional

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Trading Exchange API", version="1.0.0")

# --- Временное хранилище (In-Memory DB) ---
API_KEYS = {"test-qa-token": "user_1"}

users_db = {
    "user_1": {"balances": {"USDT": 10000.0, "BTC": 0.5, "ETH": 5.0}, "orders": {}}
}

market_prices = {"BTCUSDT": 65000.0, "ETHUSDT": 3500.0}


# --- Модели данных ---
class OrderRequest(BaseModel):
    symbol: str = Field(..., example="BTCUSDT")
    side: str = Field(..., pattern="^(BUY|SELL)$", example="BUY")
    type: str = Field(..., pattern="^(LIMIT|MARKET)$", example="LIMIT")
    quantity: float = Field(..., gt=0, example=0.1)
    price: Optional[float] = Field(None, gt=0, example=64500.0)


class OrderResponse(BaseModel):
    order_id: str
    symbol: str
    side: str
    type: str
    status: str
    quantity: float
    price: float
    created_at: float


# --- Авторизация ---
def get_current_user(
    x_api_key: str = Header(..., description="Используйте: test-qa-token")
):
    user_id = API_KEYS.get(x_api_key)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return user_id


# --- REST API Методы ---


@app.get("/api/v1/ticker/price", tags=["Market Data"])
async def get_ticker_price(symbol: Optional[str] = None):
    """Получение текущей рыночной цены"""
    if symbol:
        if symbol not in market_prices:
            raise HTTPException(status_code=400, detail="Invalid symbol")
        return {"symbol": symbol, "price": market_prices[symbol]}
    return [{"symbol": k, "price": v} for k, v in market_prices.items()]


@app.get("/api/v1/depth", tags=["Market Data"])
async def get_order_book(symbol: str = "BTCUSDT", limit: int = 5):
    """Получение стакана ордеров (Bids/Asks)"""
    if symbol not in market_prices:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    base_price = market_prices[symbol]
    bids = [
        [round(base_price - i * 10.5, 2), round(random.uniform(0.1, 2.0), 4)]
        for i in range(1, limit + 1)
    ]
    asks = [
        [round(base_price + i * 10.5, 2), round(random.uniform(0.1, 2.0), 4)]
        for i in range(1, limit + 1)
    ]
    return {"lastUpdateId": int(time.time() * 1000), "bids": bids, "asks": asks}


@app.get("/api/v1/account", tags=["Account"])
async def get_account_info(user_id: str = Depends(get_current_user)):
    """Получение балансов пользователя"""
    return {"user_id": user_id, "balances": users_db[user_id]["balances"]}


@app.post("/api/v1/order", response_model=OrderResponse, tags=["Trading"])
async def create_order(order: OrderRequest, user_id: str = Depends(get_current_user)):
    """Создание ордера (MARKET исполняется сразу, LIMIT вешается в статусе NEW)"""
    if order.symbol not in market_prices:
        raise HTTPException(status_code=400, detail="Unsupported symbol")
    if order.type == "LIMIT" and not order.price:
        raise HTTPException(
            status_code=400, detail="Price is required for LIMIT orders"
        )

    base_asset = order.symbol.replace("USDT", "")
    quote_asset = "USDT"
    exec_price = market_prices[order.symbol] if order.type == "MARKET" else order.price
    total_cost = exec_price * order.quantity
    balances = users_db[user_id]["balances"]

    # Проверка баланса и списание/зачисление средств
    if order.side == "BUY":
        if balances[quote_asset] < total_cost:
            raise HTTPException(status_code=400, detail="Insufficient USDT balance")
        balances[quote_asset] = round(balances[quote_asset] - total_cost, 4)
        if order.type == "MARKET":
            balances[base_asset] = round(balances[base_asset] + order.quantity, 4)
    elif order.side == "SELL":
        if balances[base_asset] < order.quantity:
            raise HTTPException(
                status_code=400, detail=f"Insufficient {base_asset} balance"
            )
        balances[base_asset] = round(balances[base_asset] - order.quantity, 4)
        if order.type == "MARKET":
            balances[quote_asset] = round(balances[quote_asset] + total_cost, 4)

    order_id = str(uuid.uuid4())
    status = "FILLED" if order.type == "MARKET" else "NEW"

    new_order = {
        "order_id": order_id,
        "symbol": order.symbol,
        "side": order.side,
        "type": order.type,
        "status": status,
        "quantity": order.quantity,
        "price": exec_price,
        "created_at": time.time(),
    }
    users_db[user_id]["orders"][order_id] = new_order
    return new_order


@app.get("/api/v1/order/{order_id}", response_model=OrderResponse, tags=["Trading"])
async def get_order(order_id: str, user_id: str = Depends(get_current_user)):
    """Получение статуса конкретного ордера"""
    order = users_db[user_id]["orders"].get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@app.delete("/api/v1/order/{order_id}", response_model=OrderResponse, tags=["Trading"])
async def cancel_order(order_id: str, user_id: str = Depends(get_current_user)):
    """Отмена активного ордера и возврат замороженных средств"""
    order = users_db[user_id]["orders"].get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order["status"] != "NEW":
        raise HTTPException(
            status_code=400, detail=f"Cannot cancel order in status {order['status']}"
        )

    order["status"] = "CANCELED"
    base_asset = order["symbol"].replace("USDT", "")
    balances = users_db[user_id]["balances"]

    if order["side"] == "BUY":
        balances["USDT"] = round(
            balances["USDT"] + (order["price"] * order["quantity"]), 4
        )
    else:
        balances[base_asset] = round(balances[base_asset] + order["quantity"], 4)

    return order


@app.post("/api/v1/test/reset", tags=["QA Helpers"])
async def reset_state():
    """Специальный эндпоинт для QA: сброс состояния БД перед прогоном тестов"""
    users_db["user_1"]["balances"] = {"USDT": 10000.0, "BTC": 0.5, "ETH": 5.0}
    users_db["user_1"]["orders"].clear()
    market_prices["BTCUSDT"] = 65000.0
    market_prices["ETHUSDT"] = 3500.0
    return {"status": "State reset successful"}


# --- WebSocket Стримы ---


@app.websocket("/ws/ticker/{symbol}")
async def websocket_ticker(websocket: WebSocket, symbol: str):
    """Стрим обновления цены в реальном времени (раз в 1 секунду)"""
    if symbol not in market_prices:
        await websocket.close(code=1008, reason="Invalid symbol")
        return
    await websocket.accept()
    try:
        while True:
            # Имитация колебания рынка +-0.2%
            change = random.uniform(-0.002, 0.002)
            market_prices[symbol] = round(market_prices[symbol] * (1 + change), 2)
            payload = {
                "e": "24hrTicker",
                "E": int(time.time() * 1000),
                "s": symbol,
                "c": market_prices[symbol],
            }
            await websocket.send_json(payload)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
