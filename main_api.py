# main_api.py
import time
import threading
import pandas as pd
from datetime import datetime
import pytz
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from indicators import add_ema
from strategy import bullish_crossover
from data import get_candles_zk
from utils import resolve_ce_pe_by_strikes
from zerodha_client import get_kite
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

IST = pytz.timezone("Asia/Kolkata")

# ── Global State ──
algo_state = {
    "running": False, "call_entry": None, "put_entry": None,
    "call_symbol": None, "put_symbol": None, "qty": None,
    "profit_points": None, "stoploss_points": None,
    "expiry": None, "logs": [], "pnl": 0.0,
    "access_token": None, "dry_run": True,
    "logged_in": False, "user_name": None,
}

algo_thread = None
stop_flag   = threading.Event()


# ── Model ──
class AlgoConfig(BaseModel):
    call_strike:     int
    put_strike:      int
    lots:            int
    profit_points:   int
    stoploss_points: int
    timeframe:       str
    dry_run:         bool = True


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

def place_order(kite, symbol, qty, transaction_type):
    action = "BUY" if transaction_type == kite.TRANSACTION_TYPE_BUY else "SELL"
    if algo_state["dry_run"]:
        log(f"[DRY RUN] {action} {qty} x {symbol}")
        return "DRY_RUN"
    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=kite.EXCHANGE_BFO,
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
    TF_MAP   = {"3m": "3minute", "5m": "5minute", "15m": "15minute"}
    ZK_TF    = TF_MAP[config.timeframe]
    LOT_SIZE = 20
    QTY      = config.lots * LOT_SIZE

    try:
        kite = get_kite()
    except Exception as e:
        log(f"[ERROR] Login failed: {e}")
        algo_state["running"] = False
        return

    try:
        CALL_SYMBOL, PUT_SYMBOL, CE_TOKEN, PE_TOKEN, EXPIRY = resolve_ce_pe_by_strikes(
            kite, config.call_strike, config.put_strike
        )
    except Exception as e:
        log(f"[ERROR] Failed to resolve contracts: {e}")
        algo_state["running"] = False
        return

    algo_state["call_symbol"]     = CALL_SYMBOL
    algo_state["put_symbol"]      = PUT_SYMBOL
    algo_state["qty"]             = QTY
    algo_state["profit_points"]   = config.profit_points
    algo_state["stoploss_points"] = config.stoploss_points
    algo_state["expiry"]          = str(EXPIRY)

    mode = "DRY RUN" if config.dry_run else "LIVE"
    log(f"Logged in as {algo_state['user_name']} | Algo started | Mode: {mode} | CE: {CALL_SYMBOL} | PE: {PUT_SYMBOL} | Qty: {QTY} | Expiry: {EXPIRY}")

    init_candles = get_candles_zk(kite, CE_TOKEN, ZK_TF)
    last_seen_candle_time = pd.DataFrame(init_candles).iloc[-1]["date"] if init_candles else None

    call_entry = None
    put_entry  = None

    while not stop_flag.is_set():
        try:
            if not is_market_open():
                time.sleep(60)
                continue

            ce_candles = get_candles_zk(kite, CE_TOKEN, ZK_TF)
            pe_candles = get_candles_zk(kite, PE_TOKEN, ZK_TF)

            if not ce_candles or not pe_candles:
                time.sleep(60)
                continue

            ce_df = add_ema(pd.DataFrame(ce_candles))
            pe_df = add_ema(pd.DataFrame(pe_candles))

            current_candle_time = ce_df.iloc[-1]["date"]

            if current_candle_time == last_seen_candle_time:
                time.sleep(10)
                continue

            last_seen_candle_time = current_candle_time

            ce_signal = bullish_crossover(ce_df)
            pe_signal = bullish_crossover(pe_df)

            if ce_signal and call_entry is None:
                place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_BUY)
                call_entry = ce_df.iloc[-2]["close"]
                algo_state["call_entry"] = call_entry
                log(f"[BUY - CE] {CALL_SYMBOL} | Qty: {QTY} | Price: ₹{call_entry}")

            if pe_signal and put_entry is None:
                place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_BUY)
                put_entry = pe_df.iloc[-2]["close"]
                algo_state["put_entry"] = put_entry
                log(f"[BUY - PE] {PUT_SYMBOL} | Qty: {QTY} | Price: ₹{put_entry}")

            while not stop_flag.is_set():
                if not is_market_open():
                    break

                if is_eod():
                    if call_entry is not None:
                        eod_ltp = kite.ltp(f"BFO:{CALL_SYMBOL}")[f"BFO:{CALL_SYMBOL}"]["last_price"]
                        place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                        pnl = (eod_ltp - call_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - EOD] {CALL_SYMBOL} | Price: ₹{eod_ltp} | PnL: ₹{pnl:.2f}")
                        call_entry = None
                        algo_state["call_entry"] = None
                    if put_entry is not None:
                        eod_ltp = kite.ltp(f"BFO:{PUT_SYMBOL}")[f"BFO:{PUT_SYMBOL}"]["last_price"]
                        place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                        pnl = (eod_ltp - put_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - EOD] {PUT_SYMBOL} | Price: ₹{eod_ltp} | PnL: ₹{pnl:.2f}")
                        put_entry = None
                        algo_state["put_entry"] = None
                    break

                if call_entry is not None:
                    call_ltp = kite.ltp(f"BFO:{CALL_SYMBOL}")[f"BFO:{CALL_SYMBOL}"]["last_price"]
                    if call_ltp >= call_entry + config.profit_points:
                        place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                        pnl = (call_ltp - call_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - TARGET] {CALL_SYMBOL} | Price: ₹{call_ltp} | PnL: ₹{pnl:.2f}")
                        call_entry = None
                        algo_state["call_entry"] = None
                    elif call_ltp <= call_entry - config.stoploss_points:
                        place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                        pnl = (call_ltp - call_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - STOPLOSS] {CALL_SYMBOL} | Price: ₹{call_ltp} | PnL: ₹{pnl:.2f}")
                        call_entry = None
                        algo_state["call_entry"] = None

                if put_entry is not None:
                    put_ltp = kite.ltp(f"BFO:{PUT_SYMBOL}")[f"BFO:{PUT_SYMBOL}"]["last_price"]
                    if put_ltp >= put_entry + config.profit_points:
                        place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                        pnl = (put_ltp - put_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - TARGET] {PUT_SYMBOL} | Price: ₹{put_ltp} | PnL: ₹{pnl:.2f}")
                        put_entry = None
                        algo_state["put_entry"] = None
                    elif put_ltp <= put_entry - config.stoploss_points:
                        place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                        pnl = (put_ltp - put_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - STOPLOSS] {PUT_SYMBOL} | Price: ₹{put_ltp} | PnL: ₹{pnl:.2f}")
                        put_entry = None
                        algo_state["put_entry"] = None

                ce_candles_check = get_candles_zk(kite, CE_TOKEN, ZK_TF)
                if ce_candles_check:
                    new_time = pd.DataFrame(ce_candles_check).iloc[-1]["date"]
                    if new_time != last_seen_candle_time:
                        break

                time.sleep(2)

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
    algo_state["running"]      = False
    algo_state["logged_in"]    = False
    algo_state["user_name"]    = None
    algo_state["access_token"] = None
    algo_state["logs"]         = []
    algo_state["pnl"]          = 0.0
    algo_state["call_entry"]   = None
    algo_state["put_entry"]    = None
    return {"status": "logged out"}


# ── Start algo ──
@app.post("/start")
def start_algo(config: AlgoConfig):
    global algo_thread, stop_flag

    if not algo_state["logged_in"]:
        return {"status": "error", "message": "Please login with Zerodha first."}

    if algo_state["running"]:
        return {"status": "already running"}

    algo_state["dry_run"]    = config.dry_run
    stop_flag                = threading.Event()
    algo_state["running"]    = True
    algo_state["logs"]       = []
    algo_state["pnl"]        = 0.0
    algo_state["call_entry"] = None
    algo_state["put_entry"]  = None

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
        "running":         algo_state["running"],
        "call_symbol":     algo_state["call_symbol"],
        "put_symbol":      algo_state["put_symbol"],
        "call_entry":      algo_state["call_entry"],
        "put_entry":       algo_state["put_entry"],
        "qty":             algo_state["qty"],
        "profit_points":   algo_state["profit_points"],
        "stoploss_points": algo_state["stoploss_points"],
        "expiry":          algo_state["expiry"],
        "pnl":             algo_state["pnl"],
        "dry_run":         algo_state["dry_run"],
        "logs":            algo_state["logs"][-50:],
    }