# main_api.py
import time
import threading
import pandas as pd
from datetime import datetime
from contextlib import asynccontextmanager
import pytz
import schedule

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from indicators import add_ema
from strategy import bullish_crossover
from data import get_candles_zk
from utils import resolve_ce_pe_by_strikes
from zerodha_client import get_kite
from auto_login import get_access_token

IST = pytz.timezone("Asia/Kolkata")

# ── Daily scheduler ──
def scheduled_login():
    print("Scheduled login running...")
    try:
        get_access_token()
        print("Token refreshed successfully.")
    except Exception as e:
        print(f"Scheduled login failed: {e}")

def run_scheduler():
    schedule.every().day.at("08:30").do(scheduled_login)
    while True:
        schedule.run_pending()
        time.sleep(60)

# ── Auto-login on startup ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Server starting. Auto-logging into Zerodha...")
    try:
        get_access_token()
        print("Login successful.")
    except Exception as e:
        print(f"Auto-login failed: {e}")
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    print("Daily scheduler started (runs at 08:30 IST).")
    yield

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Global State ──
algo_state = {
    "running": False, "call_entry": None, "put_entry": None,
    "call_symbol": None, "put_symbol": None, "qty": None,
    "profit_points": None, "stoploss_points": None,
    "expiry": None, "logs": [], "pnl": 0.0,
}

algo_thread = None
stop_flag   = threading.Event()

# ── Input Model (no access_token) ──
class AlgoConfig(BaseModel):
    call_strike:     int
    put_strike:      int
    lots:            int
    profit_points:   int
    stoploss_points: int
    timeframe:       str

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

def run_algo(config: AlgoConfig):
    TF_MAP   = {"3m": "3minute", "5m": "5minute", "15m": "15minute"}
    ZK_TF    = TF_MAP[config.timeframe]
    LOT_SIZE = 20
    QTY      = config.lots * LOT_SIZE

    try:
        kite = get_kite()
        log(f"Logged in as: {kite.profile()['user_name']}")
    except Exception as e:
        log(f"Login failed: {e}")
        algo_state["running"] = False
        return

    try:
        CALL_SYMBOL, PUT_SYMBOL, CE_TOKEN, PE_TOKEN, EXPIRY = resolve_ce_pe_by_strikes(
            kite, config.call_strike, config.put_strike
        )
    except Exception as e:
        log(f"Failed to resolve contracts: {e}")
        algo_state["running"] = False
        return

    algo_state["call_symbol"]     = CALL_SYMBOL
    algo_state["put_symbol"]      = PUT_SYMBOL
    algo_state["qty"]             = QTY
    algo_state["profit_points"]   = config.profit_points
    algo_state["stoploss_points"] = config.stoploss_points
    algo_state["expiry"]          = str(EXPIRY)

    log(f"CE: {CALL_SYMBOL} | PE: {PUT_SYMBOL} | Expiry: {EXPIRY} | Qty: {QTY}")

    init_candles = get_candles_zk(kite, CE_TOKEN, ZK_TF)
    last_seen_candle_time = pd.DataFrame(init_candles).iloc[-1]["date"] if init_candles else None
    log(f"Startup candle time: {last_seen_candle_time}")

    call_entry = None
    put_entry  = None

    while not stop_flag.is_set():
        try:
            if not is_market_open():
                log("Market closed. Sleeping 60s...")
                time.sleep(60)
                continue

            ce_candles = get_candles_zk(kite, CE_TOKEN, ZK_TF)
            pe_candles = get_candles_zk(kite, PE_TOKEN, ZK_TF)

            if not ce_candles or not pe_candles:
                log("No candle data yet. Waiting...")
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
                call_entry = ce_df.iloc[-2]["close"]
                algo_state["call_entry"] = call_entry
                log(f"[BUY - CE] {CALL_SYMBOL} | Qty: {QTY} | Price: Rs.{call_entry}")

            if pe_signal and put_entry is None:
                put_entry = pe_df.iloc[-2]["close"]
                algo_state["put_entry"] = put_entry
                log(f"[BUY - PE] {PUT_SYMBOL} | Qty: {QTY} | Price: Rs.{put_entry}")

            while not stop_flag.is_set():
                if not is_market_open():
                    break

                if is_eod():
                    if call_entry is not None:
                        eod_ltp = kite.ltp(f"BFO:{CALL_SYMBOL}")[f"BFO:{CALL_SYMBOL}"]["last_price"]
                        pnl = (eod_ltp - call_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - EOD] {CALL_SYMBOL} | Price: Rs.{eod_ltp} | PnL: Rs.{pnl:.2f}")
                        call_entry = None
                        algo_state["call_entry"] = None
                    if put_entry is not None:
                        eod_ltp = kite.ltp(f"BFO:{PUT_SYMBOL}")[f"BFO:{PUT_SYMBOL}"]["last_price"]
                        pnl = (eod_ltp - put_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - EOD] {PUT_SYMBOL} | Price: Rs.{eod_ltp} | PnL: Rs.{pnl:.2f}")
                        put_entry = None
                        algo_state["put_entry"] = None
                    break

                if call_entry is not None:
                    call_ltp = kite.ltp(f"BFO:{CALL_SYMBOL}")[f"BFO:{CALL_SYMBOL}"]["last_price"]
                    if call_ltp >= call_entry + config.profit_points:
                        pnl = (call_ltp - call_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - TARGET] {CALL_SYMBOL} | Price: Rs.{call_ltp} | PnL: Rs.{pnl:.2f}")
                        call_entry = None
                        algo_state["call_entry"] = None
                    elif call_ltp <= call_entry - config.stoploss_points:
                        pnl = (call_ltp - call_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - STOPLOSS] {CALL_SYMBOL} | Price: Rs.{call_ltp} | PnL: Rs.{pnl:.2f}")
                        call_entry = None
                        algo_state["call_entry"] = None

                if put_entry is not None:
                    put_ltp = kite.ltp(f"BFO:{PUT_SYMBOL}")[f"BFO:{PUT_SYMBOL}"]["last_price"]
                    if put_ltp >= put_entry + config.profit_points:
                        pnl = (put_ltp - put_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - TARGET] {PUT_SYMBOL} | Price: Rs.{put_ltp} | PnL: Rs.{pnl:.2f}")
                        put_entry = None
                        algo_state["put_entry"] = None
                    elif put_ltp <= put_entry - config.stoploss_points:
                        pnl = (put_ltp - put_entry) * QTY
                        algo_state["pnl"] += pnl
                        log(f"[SELL - STOPLOSS] {PUT_SYMBOL} | Price: Rs.{put_ltp} | PnL: Rs.{pnl:.2f}")
                        put_entry = None
                        algo_state["put_entry"] = None

                ce_candles_check = get_candles_zk(kite, CE_TOKEN, ZK_TF)
                if ce_candles_check:
                    new_time = pd.DataFrame(ce_candles_check).iloc[-1]["date"]
                    if new_time != last_seen_candle_time:
                        break

                time.sleep(2)

        except Exception as e:
            log(f"Error: {e}")
            time.sleep(10)

    algo_state["running"] = False
    log("Algo stopped.")

@app.post("/start")
def start_algo(config: AlgoConfig):
    global algo_thread, stop_flag
    if algo_state["running"]:
        return {"status": "already running"}
    stop_flag.clear()
    algo_state["running"]    = True
    algo_state["logs"]       = []
    algo_state["pnl"]        = 0.0
    algo_state["call_entry"] = None
    algo_state["put_entry"]  = None
    algo_thread = threading.Thread(target=run_algo, args=(config,), daemon=True)
    algo_thread.start()
    return {"status": "started"}

@app.post("/stop")
def stop_algo():
    stop_flag.set()
    return {"status": "stopping"}

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
        "logs":            algo_state["logs"][-50:],
    }

@app.get("/logs")
def get_logs():
    return {"logs": algo_state["logs"]}

@app.get("/")
def root():
    return {"message": "Sensex Algo API is running"}