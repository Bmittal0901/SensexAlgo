# main_api.py
"""
FastAPI service for the dashboard / remote control. This is what the
Procfile's `uvicorn main_api:app` actually runs.

No blocking input() calls anywhere in this module -- the access token and
all trade parameters arrive in the POST /api/start request body instead
of being typed at a terminal. All order-placing logic lives in
bot_engine.TradingBot, shared with main.py, so this file is just the
HTTP surface on top of it.

Run locally:
    uvicorn main_api:app --reload
"""
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from bot_engine import TradingBot, env_dry_run
from zerodha_client import get_kite

app = FastAPI(title="SensexAlgo API")

# Tighten allow_origins to your actual dashboard origin before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_bot: Optional[TradingBot] = None
_RUNNING_STATES = {"resolving", "waiting_for_market", "entering", "monitoring"}


class StartRequest(BaseModel):
    access_token: str
    index: str = Field(pattern="^(SENSEX|NIFTY)$")
    expiry: str  # YYYY-MM-DD, must be a currently-listed expiry
    buy_ce_strike: int
    buy_pe_strike: int
    sell_ce_strike: int
    sell_pe_strike: int
    buy_lots: int = Field(gt=0)
    lot_size: int = Field(gt=0)
    max_loss: float = Field(gt=0)
    per_leg_stop_loss: Optional[float] = None
    per_leg_target: Optional[float] = None
    square_off_time: str = "15:20"
    dry_run: Optional[bool] = None  # omit to fall back to the DRY_RUN env var


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/start")
def start(req: StartRequest):
    global _bot

    if _bot is not None and _bot.status in _RUNNING_STATES:
        raise HTTPException(400, "A session is already running. Stop it before starting a new one.")

    try:
        kite = get_kite(req.access_token)
    except Exception as e:
        raise HTTPException(400, f"Could not initialize Zerodha session: {e}")

    config = {
        "index": req.index,
        "expiry": req.expiry,
        "buy_ce_strike": req.buy_ce_strike,
        "buy_pe_strike": req.buy_pe_strike,
        "sell_ce_strike": req.sell_ce_strike,
        "sell_pe_strike": req.sell_pe_strike,
        "buy_lots": req.buy_lots,
        "lot_size": req.lot_size,
        "max_loss": req.max_loss,
        "per_leg_stop_loss": req.per_leg_stop_loss,
        "per_leg_target": req.per_leg_target,
        "square_off_time": req.square_off_time,
        "dry_run": req.dry_run if req.dry_run is not None else env_dry_run(),
    }

    _bot = TradingBot(kite, config)
    _bot.start()
    return {"message": "Bot started.", "dry_run": config["dry_run"]}


@app.get("/api/status")
def status():
    if _bot is None:
        return {"status": "idle"}
    return _bot.snapshot()


@app.post("/api/stop")
def stop():
    if _bot is None or _bot.status not in _RUNNING_STATES:
        raise HTTPException(400, "No session is currently running.")
    _bot.request_stop()
    return {"message": "Stop requested."}