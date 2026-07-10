# main_api.py
import time
import threading
from datetime import datetime
import pytz
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from strategy import LEG_DIRECTIONS, compute_combined_loss, should_exit
from utils import resolve_multi_leg_symbols
from zerodha_client import get_kite
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

IST = pytz.timezone("Asia/Kolkata")

LEGS = ["BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE"]
SELL_LOT_MULTIPLIER = 3

# ── Global State ──
algo_state = {
    "running": False,
    "legs": {},              # leg -> {symbol, qty, entry, current}
    "combined_loss": 0.0,
    "max_loss": None,
    "expiry": None,
    "index": None,
    "logs": [], "pnl": 0.0,
    "access_token": None, "dry_run": True,
    "logged_in": False, "user_name": None,
}

algo_thread = None
stop_flag   = threading.Event()


# ── Model ──
class AlgoConfig(BaseModel):
    index:            str   # "SENSEX" or "NIFTY"
    expiry:           str   # "YYYY-MM-DD"
    buy_strike:       int   # BUY_CE + BUY_PE strike
    sell_ce_strike:   int
    sell_pe_strike:   int
    buy_lots:         int
    lot_size:         int
    max_loss:         float
    dry_run:          bool = True


# ── Helpers ──
def log(msg: str):
    timestamp = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    algo_state["logs"].append(entry)
    print(entry)

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close

def is_eod():
    now = datetime.now(IST)
    return now >= now.replace(hour=15, minute=20, second=0, microsecond=0)

def get_ltp(kite, exchange, symbol):
    key = f"{exchange}:{symbol}"
    return kite.ltp(key)[key]["last_price"]

def place_order(kite, exchange, symbol, qty, transaction_type):
    action = "BUY" if transaction_type == kite.TRANSACTION_TYPE_BUY else "SELL"
    if algo_state["dry_run"]:
        log(f"[DRY RUN] {action} {qty} x {symbol}")
        return "DRY_RUN"
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=qty,
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET,
        )
        log(f"[ORDER PLACED] {action} {qty} x {symbol} | ID: {order_id}")
        return order_id
    except Exception as e:
        log(f"[ORDER FAILED] {action} {symbol} | Error: {e}")
        return None


# ── Algo Thread ──
def run_algo(config: AlgoConfig):
    try:
        kite = get_kite()
    except Exception as e:
        log(f"[ERROR] Login failed: {e}")
        algo_state["running"] = False
        return

    buy_lots  = config.buy_lots
    sell_lots = buy_lots * SELL_LOT_MULTIPLIER
    qty_buy   = buy_lots * config.lot_size
    qty_sell  = sell_lots * config.lot_size

    qtys = {
        "BUY_CE":  qty_buy,
        "BUY_PE":  qty_buy,
        "SELL_CE": qty_sell,
        "SELL_PE": qty_sell,
    }

    entry_txn = {
        leg: (kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL)
        for leg, direction in LEG_DIRECTIONS.items()
    }
    exit_txn = {
        leg: (kite.TRANSACTION_TYPE_SELL if direction == "BUY" else kite.TRANSACTION_TYPE_BUY)
        for leg, direction in LEG_DIRECTIONS.items()
    }

    try:
        legs, exchange = resolve_multi_leg_symbols(
            kite, config.index, config.expiry,
            config.buy_strike, config.sell_ce_strike, config.sell_pe_strike
        )
    except Exception as e:
        log(f"[ERROR] Failed to resolve contracts: {e}")
        algo_state["running"] = False
        return

    algo_state["index"]    = config.index
    algo_state["expiry"]   = config.expiry
    algo_state["max_loss"] = config.max_loss
    algo_state["legs"] = {
        leg: {"symbol": legs[leg]["symbol"], "qty": qtys[leg], "entry": None, "current": None}
        for leg in LEGS
    }

    mode = "DRY RUN" if config.dry_run else "LIVE"
    log(f"Logged in as {algo_state['user_name']} | Mode: {mode} | Expiry: {config.expiry}")
    for leg in LEGS:
        log(f"  {leg}: {legs[leg]['symbol']} (qty {qtys[leg]})")

    # ── Wait for market open, then enter all 4 legs ──
    while not stop_flag.is_set() and not is_market_open():
        time.sleep(30)

    if stop_flag.is_set():
        algo_state["running"] = False
        return

    entry_prices = {}
    for leg in LEGS:
        symbol = legs[leg]["symbol"]
        place_order(kite, exchange, symbol, qtys[leg], entry_txn[leg])
        price = get_ltp(kite, exchange, symbol)
        entry_prices[leg] = price
        algo_state["legs"][leg]["entry"] = price
        algo_state["legs"][leg]["current"] = price
        log(f"[{leg}] {symbol} | Qty: {qtys[leg]} | Entry: ₹{price}")

    log(">> ALL 4 LEGS ENTERED")

    # ── Monitor loop ──
    while not stop_flag.is_set():
        try:
            if not is_market_open():
                time.sleep(60)
                continue

            current_prices = {}
            for leg in LEGS:
                price = get_ltp(kite, exchange, legs[leg]["symbol"])
                current_prices[leg] = price
                algo_state["legs"][leg]["current"] = price

            combined_loss = compute_combined_loss(entry_prices, current_prices, qtys)
            algo_state["combined_loss"] = combined_loss

            exit_reason = None
            if should_exit(combined_loss, config.max_loss):
                exit_reason = "MAX LOSS HIT"
            elif is_eod():
                exit_reason = "EOD"

            if exit_reason:
                for leg in LEGS:
                    symbol = legs[leg]["symbol"]
                    place_order(kite, exchange, symbol, qtys[leg], exit_txn[leg])
                    log(f"[EXIT - {exit_reason}] {leg} {symbol} | Price: ₹{current_prices[leg]}")

                algo_state["pnl"] = -combined_loss
                log(f"── Exit reason: {exit_reason} | Total PnL: ₹{-combined_loss:.2f} ──")
                break

            time.sleep(5)

        except Exception as e:
            log(f"[ERROR] {e}")
            time.sleep(10)

    algo_state["running"] = False


# ── FastAPI App ──
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Serve dashboard ──
@app.get("/", response_class=HTMLResponse)
def root():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return f.read()


# ── Get Zerodha login URL ──
@app.get("/zerodha-login-url")
def zerodha_login_url():
    kite = KiteConnect(api_key=API_KEY)
    return {"url": kite.login_url()}


# ── Zerodha OAuth callback ──
@app.get("/callback")
def zerodha_callback(request: Request):
    request_token = request.query_params.get("request_token")
    status        = request.query_params.get("status")

    if status != "success" or not request_token:
        return HTMLResponse("""
            <html><body style='background:#070b14;color:#ff3d57;font-family:monospace;
            display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>
            <div style='text-align:center'>
                <h2>Login Failed</h2>
                <p style='color:#4a6080;margin-top:10px'>Please close this and try again.</p>
            </div></body></html>
        """)

    try:
        kite = KiteConnect(api_key=API_KEY)
        session_data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = session_data["access_token"]

        with open("access_token.txt", "w") as f:
            f.write(access_token)

        algo_state["access_token"] = access_token
        algo_state["logged_in"]    = True

        kite.set_access_token(access_token)
        profile = kite.profile()
        algo_state["user_name"] = profile["user_name"]

        return RedirectResponse(url="/")

    except Exception as e:
        return HTMLResponse(f"""
            <html><body style='background:#070b14;color:#ff3d57;font-family:monospace;
            display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>
            <div style='text-align:center'>
                <h2>Error</h2>
                <p style='color:#4a6080;margin-top:10px'>{str(e)}</p>
            </div></body></html>
        """)


# ── Auth status ──
@app.get("/auth-status")
def auth_status():
    return {
        "logged_in": algo_state["logged_in"],
        "user_name": algo_state["user_name"],
    }


# ── Logout ──
@app.post("/logout")
def logout():
    stop_flag.set()
    log("User logged out. Algo stopped.")
    algo_state["running"]       = False
    algo_state["logged_in"]     = False
    algo_state["user_name"]     = None
    algo_state["access_token"]  = None
    algo_state["logs"]          = []
    algo_state["pnl"]           = 0.0
    algo_state["legs"]          = {}
    algo_state["combined_loss"] = 0.0
    return {"status": "logged out"}


# ── Start algo ──
@app.post("/start")
def start_algo(config: AlgoConfig):
    global algo_thread, stop_flag

    if not algo_state["logged_in"]:
        return {"status": "error", "message": "Please login with Zerodha first."}

    if algo_state["running"]:
        return {"status": "already running"}

    algo_state["dry_run"]       = config.dry_run
    stop_flag                   = threading.Event()
    algo_state["running"]       = True
    algo_state["logs"]          = []
    algo_state["pnl"]           = 0.0
    algo_state["legs"]          = {}
    algo_state["combined_loss"] = 0.0

    algo_thread = threading.Thread(target=run_algo, args=(config,), daemon=True)
    algo_thread.start()
    return {"status": "started"}


# ── Stop algo ──
@app.post("/stop")
def stop_algo():
    stop_flag.set()
    algo_state["running"] = False
    return {"status": "stopping"}


# ── Status ──
@app.get("/status")
def get_status():
    return {
        "running":       algo_state["running"],
        "index":         algo_state["index"],
        "expiry":        algo_state["expiry"],
        "max_loss":      algo_state["max_loss"],
        "combined_loss": algo_state["combined_loss"],
        "legs":          algo_state["legs"],
        "pnl":           algo_state["pnl"],
        "dry_run":       algo_state["dry_run"],
        "logs":          algo_state["logs"][-50:],
    }