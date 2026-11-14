from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx
import os

app = FastAPI(title="NexTrade API")

os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_frontend():
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "NexTrade API running", "message": "Place your index.html inside the static/ folder."}

@app.get("/api/prices")
async def get_live_prices():
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": "bitcoin,ethereum,solana",
        "vs_currencies": "usd",
        "include_24hr_change": "true"
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(url, params=params)
            res.raise_for_status()
            return {"source": "live", "data": res.json()}
    except Exception as e:
        return {"source": "fallback", "error": str(e)}
