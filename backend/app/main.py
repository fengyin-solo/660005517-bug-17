import asyncio, time, random, math, json, threading
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Grid Trading Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ACTIVE_CLIENTS = []
SIM_RUNNING = True
current_price = 100.0
ticks_history = []

class GridConfig(BaseModel):
    lowerPrice: float = 95
    upperPrice: float = 115
    gridCount: int = 20
    capitalPerGrid: float = 1000
    initialCapital: float = 100000


def simulate_market():
    global current_price, ticks_history
    price = 100.0
    while SIM_RUNNING:
        drift = 0.005 * math.sin(time.time() * 0.05)
        price += random.gauss(drift, 0.3)
        price = max(80, min(130, price))
        current_price = price
        tick = {
            "time": time.strftime("%H:%M:%S"),
            "price": round(price, 2),
            "bid": round(price - random.uniform(0.01, 0.05), 2),
            "ask": round(price + random.uniform(0.01, 0.05), 2),
            "volume": random.randint(100, 5000)
        }
        ticks_history.append(tick)
        if len(ticks_history) > 200:
            ticks_history = ticks_history[-200:]

        # Order book
        bids = [[round(price - 0.01 * i, 2), random.randint(100, 1000)] for i in range(1, 11)]
        asks = [[round(price + 0.01 * i, 2), random.randint(100, 1000)] for i in range(1, 11)]
        order_book = {"bids": bids, "asks": asks, "midPrice": price, "spread": round(asks[0][0] - bids[0][0], 2)}

        payload = json.dumps({"ticks": ticks_history[-60:], "orderBook": order_book})
        for ws in ACTIVE_CLIENTS:
            try: asyncio.run_coroutine_threadsafe(ws.send_text(payload), asyncio.get_event_loop())
            except: pass
        time.sleep(0.5)


@app.on_event("startup")
async def startup():
    threading.Thread(target=simulate_market, daemon=True).start()


def _empty_stats(initial_capital: float):
    """Stats for runs that produce no trades: everything stays at a sane zero."""
    equity_curve = [round(float(initial_capital), 2)]
    return {
        "orders": [],
        "tradeCount": 0,
        "finalEquity": equity_curve[-1],
        "totalProfit": 0.0,
        "returnRate": 0.0,
        "sharpeRatio": 0.0,
        "maxDrawdown": 0.0,
        "winRate": 0.0,
        "equityCurve": equity_curve,
    }


@app.post("/api/backtest")
def run_backtest(config: GridConfig):
    initial_capital = float(config.initialCapital)

    # Degenerate parameters cannot produce a meaningful run; return sane zeros
    # instead of dividing by a zero/negative grid width.
    if config.gridCount <= 0 or config.upperPrice <= config.lowerPrice \
            or config.capitalPerGrid <= 0 or initial_capital <= 0:
        return _empty_stats(initial_capital)

    step = (config.upperPrice - config.lowerPrice) / config.gridCount
    grid_prices = [config.lowerPrice + i * step for i in range(config.gridCount + 1)]

    # Simulate prices; seed both generators so parameter changes can be compared run-to-run.
    np.random.seed(42)
    random.seed(42)
    prices = [100.0]
    for _ in range(200):
        prices.append(prices[-1] + random.gauss(0, 1.2))
    prices = [max(70, min(140, p)) for p in prices]

    buy_grids = {}  # grid price -> filled BUY quantity awaiting a SELL
    orders = []
    cash = initial_capital
    holdings = 0.0
    equity_curve = [round(initial_capital, 2)]
    order_id = 0

    for p in prices:
        for gp in grid_prices:
            # Buy signal
            if p <= gp and gp not in buy_grids and cash >= config.capitalPerGrid:
                qty = config.capitalPerGrid / gp
                cash -= config.capitalPerGrid
                holdings += qty
                buy_grids[gp] = qty
                order_id += 1
                orders.append({"id": order_id, "price": round(gp, 2), "side": "BUY", "quantity": round(qty, 2), "status": "FILLED", "profit": 0.0})

            # Sell signal: close the position opened one grid below
            upper_gp = gp + step * 0.5
            if p >= upper_gp and gp in buy_grids:
                qty = buy_grids.pop(gp)
                buy_price = gp
                sell_price = gp + step * 0.5
                profit = qty * (sell_price - buy_price)
                cash += qty * sell_price
                holdings -= qty
                order_id += 1
                orders.append({"id": order_id, "price": round(sell_price, 2), "side": "SELL", "quantity": round(qty, 2), "status": "FILLED", "profit": round(profit, 2)})

        # This single rounded equity series is the source of truth returned to
        # the chart AND used for every performance metric below.
        equity_curve.append(round(cash + holdings * p, 2))

    # ---- Metrics: all derived from the same equity series / order detail ----
    final_equity = equity_curve[-1]
    total_profit = round(final_equity - initial_capital, 2)
    return_rate = round(total_profit / initial_capital * 100, 2)

    # Sharpe ratio from per-step returns of the equity series itself
    equity_arr = np.array(equity_curve, dtype=float)
    eq_returns = np.diff(equity_arr) / np.maximum(equity_arr[:-1], 1e-9)
    if eq_returns.size > 1 and float(np.std(eq_returns)) > 1e-12:
        sharpe = float(np.mean(eq_returns) / np.std(eq_returns) * np.sqrt(252))
    else:
        sharpe = 0.0

    # Max drawdown: rolling peak across the whole series (never the last value)
    peak = equity_arr[0]
    max_dd = 0.0
    for e in equity_arr:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100)

    # Win rate / trade count: closed (SELL) trades only. BUY records are open
    # positions and must never count as completed trades.
    sell_orders = [o for o in orders if o["side"] == "SELL"]
    trade_count = len(sell_orders)
    wins = sum(1 for o in sell_orders if o["profit"] > 0)
    win_rate = round(wins / trade_count * 100, 1) if trade_count > 0 else 0.0

    return {
        "orders": orders,
        "tradeCount": trade_count,
        "finalEquity": final_equity,
        "totalProfit": total_profit,
        "returnRate": return_rate,
        "sharpeRatio": round(sharpe, 2),
        "maxDrawdown": round(max_dd, 2),
        "winRate": win_rate,
        "equityCurve": equity_curve,
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ACTIVE_CLIENTS.append(ws)
    try:
        while True: await ws.receive_text()
    except: 
        if ws in ACTIVE_CLIENTS: ACTIVE_CLIENTS.remove(ws)