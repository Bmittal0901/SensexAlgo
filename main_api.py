# main_api.py
"""
FastAPI service for the dashboard / remote control. This is what the
Procfile's `uvicorn main_api:app` actually runs.

Auth model: this server holds the Zerodha session, not the browser.
  1. Dashboard calls GET /api/zerodha-login-url and redirects the user
     there -- that's Zerodha's own login page, so the user's password
     never touches this server or the browser's JS.
  2. Zerodha redirects back to GET /callback?request_token=...
     (this must exactly match the redirect URL registered in your Kite
     Connect app settings -- e.g. https://sensexalgo.onrender.com/callback)
  3. That handler exchanges request_token for an access_token via
     kite.generate_session() and keeps it in memory for the life of the
     process. GET /api/auth-status tells the dashboard whether that
     session exists; POST /api/logout drops it.
  4. POST /api/start no longer takes access_token in the body -- it
     just uses whatever session is currently held.

Kite access tokens expire daily, so you'll need to log in again each
morning; there's no refresh-token flow in Kite Connect.

Run locally:
    uvicorn main_api:app --reload
"""
import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from kiteconnect import KiteConnect
from pydantic import BaseModel, Field

from bot_engine import TradingBot, env_dry_run
from zerodha_config import API_KEY, API_SECRET

app = FastAPI(title="SensexAlgo API")

# Tighten allow_origins to your actual dashboard origin before going live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")

# ---------------- session state (single-user, in-memory) ----------------
_access_token: Optional[str] = None
_user_name: Optional[str] = None

_bot: Optional[TradingBot] = None
_RUNNING_STATES = {"resolving", "waiting_for_market", "entering", "monitoring"}


def _get_authed_kite() -> KiteConnect:
    if not _access_token:
        raise HTTPException(401, "Not logged in. Log in with Zerodha first.")
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(_access_token)
    return kite


@app.get("/")
def dashboard():
    """Serves dashboard.html itself."""
    return FileResponse(_DASHBOARD_PATH)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------- Zerodha login ----------------

@app.get("/api/zerodha-login-url")
def zerodha_login_url():
    kite = KiteConnect(api_key=API_KEY)
    return {"url": kite.login_url()}


@app.get("/callback")
def zerodha_callback(request_token: str, status: Optional[str] = None,
                      action: Optional[str] = None, type: Optional[str] = None):
    """Zerodha redirects the user's browser here after login. This is a
    browser navigation, not a fetch() call from dashboard.html, so it's
    exempt from CORS -- just make sure this exact path is registered as
    the redirect URL in your Kite Connect app settings."""
    global _access_token, _user_name

    if status == "cancelled":
        return RedirectResponse(url="/")

    kite = KiteConnect(api_key=API_KEY)
    try:
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Zerodha login failed: {e}")

    _access_token = session_data["access_token"]
    kite.set_access_token(_access_token)
    try:
        _user_name = kite.profile().get("user_name")
    except Exception:
        _user_name = None

    return RedirectResponse(url="/")


@app.get("/api/auth-status")
def auth_status():
    if not _access_token:
        return {"logged_in": False}
    return {"logged_in": True, "user_name": _user_name}


@app.post("/api/logout")
def logout():
    global _access_token, _user_name
    _access_token = None
    _user_name = None
    return {"message": "Logged out."}


# ---------------- bot control ----------------
class StartRequest(BaseModel):
    index: str = Field(pattern="^(SENSEX|NIFTY)$")
    expiry: str
    # lot_size intentionally hardcoded below, not a request field -- confirmed correct as of now.

    trailing_stop_enabled: bool = False
    trail_amount: float = Field(default=50, gt=0)
    target_profit: Optional[float] = None

    buy_ce_strike: Optional[int] = None
    buy_pe_strike: Optional[int] = None
    sell_ce_strike: Optional[int] = None
    sell_pe_strike: Optional[int] = None

    buy_ce_lots: int = 0
    buy_pe_lots: int = 0
    sell_ce_lots: int = 0
    sell_pe_lots: int = 0

    max_loss: float = Field(gt=0)

    per_leg_stop_loss: Optional[float] = None
    per_leg_target: Optional[float] = None

    square_off_time: str = "15:20"

    dry_run: Optional[bool] = None


_LEG_FIELDS = {
    "BUY_CE":  ("buy_ce_strike", "buy_ce_lots"),
    "BUY_PE":  ("buy_pe_strike", "buy_pe_lots"),
    "SELL_CE": ("sell_ce_strike", "sell_ce_lots"),
    "SELL_PE": ("sell_pe_strike", "sell_pe_lots"),
}


def _validate_legs(req: StartRequest):
    active = []
    for leg, (strike_field, lots_field) in _LEG_FIELDS.items():
        strike = getattr(req, strike_field)
        lots = getattr(req, lots_field)
        if strike is None:
            continue
        if lots is None or lots <= 0:
            raise HTTPException(400, f"{leg}: strike given but {lots_field} is missing or not positive.")
        active.append(leg)
    if not active:
        raise HTTPException(
            400,
            "No legs entered -- provide a strike (and matching lot count) for at least one leg."
        )
    return active


@app.post("/api/start")
def start(req: StartRequest):
    global _bot

    if _bot is not None and _bot.status in _RUNNING_STATES:
        raise HTTPException(400, "A session is already running. Stop it before starting a new one.")

    _validate_legs(req)
    if req.target_profit is not None and req.target_profit <= 0:
        raise HTTPException(400, "target_profit must be positive when set.")

    kite = _get_authed_kite()
    lot_size = 20 if req.index == "SENSEX" else 65
    config = {
        "index": req.index,
        "expiry": req.expiry,
        "buy_ce_strike": req.buy_ce_strike,
        "buy_pe_strike": req.buy_pe_strike,
        "sell_ce_strike": req.sell_ce_strike,
        "sell_pe_strike": req.sell_pe_strike,
        "buy_ce_lots": req.buy_ce_lots,
        "buy_pe_lots": req.buy_pe_lots,
        "sell_ce_lots": req.sell_ce_lots,
        "sell_pe_lots": req.sell_pe_lots,
        "lot_size": lot_size,
        "max_loss": req.max_loss,
        "per_leg_stop_loss": req.per_leg_stop_loss,
        "per_leg_target": req.per_leg_target,
        "square_off_time": req.square_off_time,
        "dry_run": req.dry_run if req.dry_run is not None else env_dry_run(),
        "trailing_stop_enabled": req.trailing_stop_enabled,
        "trail_amount": req.trail_amount,
        "target_profit": req.target_profit,
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

@app.post("/api/exit")
def exit():

    if _bot is None or _bot.status not in _RUNNING_STATES:
        raise HTTPException(400, "No session is currently running.")

    _bot.request_exit()

    return {"message": "Exit requested."}