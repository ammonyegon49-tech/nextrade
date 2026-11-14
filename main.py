from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import sqlite3, hashlib, secrets, httpx, os
from datetime import datetime

app = FastAPI(title="NexTrade API")
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
DB_PATH = "nextrade.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        token TEXT,
        balance REAL DEFAULT 10000.0,
        btc REAL DEFAULT 0.0,
        eth REAL DEFAULT 0.0,
        sol REAL DEFAULT 0.0
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        pair TEXT NOT NULL,
        side TEXT NOT NULL,
        amount REAL NOT NULL,
        qty REAL DEFAULT 0.0,
        price REAL NOT NULL,
        timestamp TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

init_db()

class AuthPayload(BaseModel):
    username: str
    password: str

class TradePayload(BaseModel):
    token: str
    asset: str
    pair: str
    side: str
    amount: float
    price: float

def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join("static", "index.html"))

@app.post("/api/auth")
async def login_or_register(payload: AuthPayload):
    uname = payload.username.strip().lower()
    if len(uname) < 3 or len(payload.password) < 4:
        raise HTTPException(status_code=400, detail="Username (3+ chars) and password (4+ chars) required.")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (uname,)).fetchone()
    token = secrets.token_hex(16)
    if user:
        if user["password_hash"] != hash_pw(payload.password):
            conn.close()
            raise HTTPException(status_code=401, detail="Invalid password.")
        conn.execute("UPDATE users SET token = ? WHERE id = ?", (token, user["id"]))
        conn.commit()
    else:
        conn.execute("INSERT INTO users (username, password_hash, token, balance, btc, eth, sol) VALUES (?, ?, ?, 10000.0, 0.0, 0.0, 0.0)",
                     (uname, hash_pw(payload.password), token))
        conn.commit()
    conn.close()
    return {"token": token, "username": uname}

@app.get("/api/portfolio")
async def get_portfolio(token: str):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="Session expired.")
    trades = conn.execute("SELECT pair, side, amount, qty, price, timestamp FROM trades WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user["id"],)).fetchall()
    conn.close()
    return {
        "username": user["username"],
        "balance": user["balance"],
        "holdings": {"bitcoin": user["btc"], "ethereum": user["eth"], "solana": user["sol"]},
        "trades": [dict(t) for t in trades]
    }

@app.post("/api/trade")
async def place_trade(payload: TradePayload):
    col_map = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol"}
    if payload.asset not in col_map or payload.amount <= 0 or payload.price <= 0:
        raise HTTPException(status_code=400, detail="Invalid order parameters.")
    col = col_map[payload.asset]
    qty = payload.amount / payload.price
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE token = ?", (payload.token,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=401, detail="Please log in to trade.")
    bal, held = user["balance"], user[col]
    if payload.side == "BUY":
        if payload.amount > bal:
            conn.close()
            raise HTTPException(status_code=400, detail="Insufficient USD balance.")
        new_bal, new_held = bal - payload.amount, held + qty
    else:
        if qty > held * 1.0001:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Insufficient {col.upper()} holdings to sell.")
        new_bal, new_held = bal + payload.amount, max(0.0, held - qty)
    ts = datetime.now().strftime("%H:%M:%S")
    conn.execute(f"UPDATE users SET balance = ?, {col} = ? WHERE id = ?", (new_bal, new_held, user["id"]))
    conn.execute("INSERT INTO trades (user_id, pair, side, amount, qty, price, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (user["id"], payload.pair, payload.side, payload.amount, qty, payload.price, ts))
    conn.commit()
    conn.close()
    return {"status": "filled"}

@app.get("/api/prices")
async def get_live_prices():
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url, params=params)
            res.raise_for_status()
            return {"source": "live", "data": res.json()}
    except Exception as e:
        return {"source": "fallback", "error": str(e)}
