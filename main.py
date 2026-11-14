import os
import sqlite3
import hashlib
import secrets
import shutil
from datetime import datetime
from typing import Optional
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="NexTrade Exchange")

os.makedirs("static/kyc_uploads", exist_ok=True)
DB_PATH = "nextrade.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            usd_balance REAL DEFAULT 10000.0,
            kyc_status TEXT DEFAULT 'UNVERIFIED',
            kyc_doc_type TEXT,
            kyc_doc_number TEXT,
            kyc_doc_file TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS holdings (
            user_id INTEGER,
            symbol TEXT,
            amount REAL DEFAULT 0.0,
            PRIMARY KEY (user_id, symbol)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            symbol TEXT,
            side TEXT,
            amount REAL,
            price REAL,
            total REAL,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

class RegisterRequest(BaseModel):
    full_name: str
    email: str
    phone: str
    password: str

class LoginRequest(BaseModel):
    identifier: str  # Email or Phone
    password: str

class TradeRequest(BaseModel):
    user_id: int
    symbol: str
    side: str
    usd_amount: float

@app.post("/api/register")
def register(req: RegisterRequest):
    email = req.email.strip().lower()
    phone = req.phone.strip()
    if not req.full_name.strip() or not email or not phone or len(req.password) < 4:
        raise HTTPException(status_code=400, detail="All fields are required (min 4 char password).")
    conn = get_db()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO users (full_name, email, phone, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (req.full_name.strip(), email, phone, hash_password(req.password), datetime.utcnow().isoformat())
        )
        user_id = c.lastrowid
        for sym in ["BTC", "ETH", "SOL"]:
            c.execute("INSERT INTO holdings (user_id, symbol, amount) VALUES (?, ?, 0.0)", (user_id, sym))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Email or phone number is already registered.")
    conn.close()
    return {"user_id": user_id, "message": "Account created. Please complete KYC verification."}

@app.post("/api/login")
def login(req: LoginRequest):
    ident = req.identifier.strip().lower()
    conn = get_db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM users WHERE (lower(email) = ? OR phone = ?) AND password_hash = ?",
        (ident, req.identifier.strip(), hash_password(req.password))
    )
    user = c.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email/phone or password.")
    return {
        "user_id": user["id"],
        "full_name": user["full_name"],
        "email": user["email"],
        "phone": user["phone"],
        "kyc_status": user["kyc_status"],
        "kyc_doc_type": user["kyc_doc_type"],
        "kyc_doc_number": user["kyc_doc_number"]
    }

@app.post("/api/kyc")
async def submit_kyc(
    user_id: int = Form(...),
    doc_type: str = Form(...),
    doc_number: str = Form(...),
    full_legal_name: str = Form(...),
    document_file: UploadFile = File(...)
):
    allowed_types = ["National Identity Card", "Passport", "Driving License", "Other Government ID"]
    if doc_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid document type selected.")
    if not doc_number.strip() or not full_legal_name.strip():
        raise HTTPException(status_code=400, detail="Document number and full legal name are required.")

    ext = os.path.splitext(document_file.filename)[1] or ".jpg"
    safe_name = f"kyc_{user_id}_{secrets.token_hex(4)}{ext}"
    file_path = os.path.join("static/kyc_uploads", safe_name)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(document_file.file, buffer)

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE users
        SET full_name = ?, kyc_status = 'VERIFIED', kyc_doc_type = ?, kyc_doc_number = ?, kyc_doc_file = ?
        WHERE id = ?
    """, (full_legal_name.strip(), doc_type, doc_number.strip().upper(), f"/static/kyc_uploads/{safe_name}", user_id))
    conn.commit()
    conn.close()
    return {"status": "VERIFIED", "doc_type": doc_type, "doc_number": doc_number.strip().upper()}

@app.get("/api/prices")
async def get_prices():
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
            data = r.json()
            return {
                "BTC": {"price": data["bitcoin"]["usd"], "change": data["bitcoin"].get("usd_24h_change", 0)},
                "ETH": {"price": data["ethereum"]["usd"], "change": data["ethereum"].get("usd_24h_change", 0)},
                "SOL": {"price": data["solana"]["usd"], "change": data["solana"].get("usd_24h_change", 0)},
            }
    except Exception:
        return {
            "BTC": {"price": 91250.0, "change": 2.4},
            "ETH": {"price": 3180.0, "change": 1.8},
            "SOL": {"price": 215.0, "change": 4.1},
        }

@app.get("/api/portfolio/{user_id}")
def get_portfolio(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    c.execute("SELECT symbol, amount FROM holdings WHERE user_id = ?", (user_id,))
    holdings = {row["symbol"]: row["amount"] for row in c.fetchall()}
    c.execute("SELECT * FROM trades WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,))
    trades = [dict(row) for row in c.fetchall()]
    conn.close()
    return {
        "user": {
            "id": user["id"],
            "full_name": user["full_name"],
            "email": user["email"],
            "phone": user["phone"],
            "usd_balance": user["usd_balance"],
            "kyc_status": user["kyc_status"],
            "kyc_doc_type": user["kyc_doc_type"],
            "kyc_doc_number": user["kyc_doc_number"],
        },
        "holdings": holdings,
        "trades": trades,
    }

@app.post("/api/trade")
async def place_trade(req: TradeRequest):
    if req.usd_amount <= 0:
        raise HTTPException(status_code=400, detail="Trade amount must be positive.")
    prices = await get_prices()
    if req.symbol not in prices:
        raise HTTPException(status_code=400, detail="Unsupported asset.")
    price = prices[req.symbol]["price"]
    coin_qty = req.usd_amount / price

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT usd_balance, kyc_status FROM users WHERE id = ?", (req.user_id,))
    user = c.fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found.")
    if user["kyc_status"] != "VERIFIED":
        conn.close()
        raise HTTPException(status_code=403, detail="Please complete KYC Identity Verification before trading.")

    c.execute("SELECT amount FROM holdings WHERE user_id = ? AND symbol = ?", (req.user_id, req.symbol))
    row = c.fetchone()
    current_coin = row["amount"] if row else 0.0

    if req.side.upper() == "BUY":
        if user["usd_balance"] < req.usd_amount:
            conn.close()
            raise HTTPException(status_code=400, detail="Insufficient USDT balance.")
        new_usd = user["usd_balance"] - req.usd_amount
        new_coin = current_coin + coin_qty
    elif req.side.upper() == "SELL":
        if current_coin < coin_qty:
            conn.close()
            raise HTTPException(status_code=400, detail=f"Insufficient {req.symbol} balance.")
        new_usd = user["usd_balance"] + req.usd_amount
        new_coin = current_coin - coin_qty
    else:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid order side.")

    c.execute("UPDATE users SET usd_balance = ? WHERE id = ?", (new_usd, req.user_id))
    c.execute("UPDATE holdings SET amount = ? WHERE user_id = ? AND symbol = ?", (new_coin, req.user_id, req.symbol))
    c.execute(
        "INSERT INTO trades (user_id, symbol, side, amount, price, total, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (req.user_id, req.symbol, req.side.upper(), coin_qty, price, req.usd_amount, datetime.utcnow().strftime("%H:%M:%S"))
    )
    conn.commit()
    conn.close()
    return {"status": "filled", "filled_qty": coin_qty, "price": price}

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def root():
    return FileResponse("static/index.html")
