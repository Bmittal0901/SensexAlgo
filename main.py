# main.py
import time
import pandas as pd
from datetime import datetime
import pytz

from inputs import get_user_inputs
from indicators import add_ema
from strategy import bullish_crossover
from data import get_candles_zk
from utils import resolve_ce_pe_by_strikes
from zerodha_client import get_kite

IST = pytz.timezone("Asia/Kolkata")

# SET TO False WHEN READY TO TRADE REAL MONEY
DRY_RUN = True

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


# ---------------- Order Placement ----------------
def place_order(kite, symbol, qty, transaction_type):
    exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
    action = "BUY" if transaction_type == kite.TRANSACTION_TYPE_BUY else "SELL"

    if DRY_RUN:
        print(f"[DRY RUN] {action} {qty} x {symbol} at {exec_time}")
        return "DRY_RUN_ORDER"

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
        print(f"[ORDER PLACED] {action} {qty} x {symbol} | Order ID: {order_id} | Time: {exec_time}")
        return order_id
    except Exception as e:
        print(f"[ORDER FAILED] {action} {symbol} | Error: {e}")
        return None


# ---------------- Zerodha Login ----------------
ACCESS_TOKEN = input("Paste Zerodha ACCESS_TOKEN for today: ").strip()
kite = get_kite(ACCESS_TOKEN)
print("Logged in as:", kite.profile()["user_name"])

if DRY_RUN:
    print("\n  DRY RUN MODE — No real orders will be placed.\n")
else:
    print("\n LIVE MODE — Real orders will be placed!\n")

# ---------------- User Inputs ----------------
user = get_user_inputs()

CALL_STRIKE     = user["CALL_STRIKE"]
PUT_STRIKE      = user["PUT_STRIKE"]
LOTS            = user["LOTS"]
LOT_SIZE        = user["LOT_SIZE"]
PROFIT_POINTS   = user["PROFIT_POINTS"]
STOPLOSS_POINTS = user["STOPLOSS_POINTS"]
TIMEFRAME       = user["TIMEFRAME"]

QTY = LOTS * LOT_SIZE

# ---------------- Auto-resolve CE/PE contracts ----------------
TF_MAP = {"3m": "3minute", "5m": "5minute", "15m": "15minute"}
ZK_TF  = TF_MAP[TIMEFRAME]

CALL_SYMBOL, PUT_SYMBOL, CE_TOKEN, PE_TOKEN, EXPIRY = resolve_ce_pe_by_strikes(
    kite, CALL_STRIKE, PUT_STRIKE
)

print("\nResolved contracts automatically:")
print("Expiry    :", EXPIRY)
print("CE        :", CALL_SYMBOL)
print("PE        :", PUT_SYMBOL)
print("Timeframe :", ZK_TF)
print("Quantity  :", QTY)

# ---------------- State ----------------
call_entry = None
put_entry  = None
total_pnl  = 0.0

# ---- Startup: skip forming candle ----
init_candles = get_candles_zk(kite, CE_TOKEN, ZK_TF)
last_seen_candle_time = pd.DataFrame(init_candles).iloc[-1]["date"] if init_candles else None
print(f"\nStartup candle time set to: {last_seen_candle_time}\n")
print("Algo started. Waiting for EMA crossover signals...\n")

# ---------------- Main Loop ----------------
while True:
    try:
        # ---- Market hours check ----
        if not is_market_open():
            print("Market closed. Sleeping 60s...")
            time.sleep(60)
            continue

        # ---- Fetch candles ----
        ce_candles = get_candles_zk(kite, CE_TOKEN, ZK_TF)
        pe_candles = get_candles_zk(kite, PE_TOKEN, ZK_TF)

        if not ce_candles or not pe_candles:
            print("No candle data yet. Waiting...")
            time.sleep(60)
            continue

        ce_df = add_ema(pd.DataFrame(ce_candles))
        pe_df = add_ema(pd.DataFrame(pe_candles))

        current_candle_time = ce_df.iloc[-1]["date"]

        # ---- Wait for new candle ----
        if current_candle_time == last_seen_candle_time:
            time.sleep(10)
            continue

        last_seen_candle_time = current_candle_time

        # ---- Check crossover on last CLOSED candle ----
        ce_signal = bullish_crossover(ce_df)
        pe_signal = bullish_crossover(pe_df)

        # ---- ENTRY ----
        if ce_signal and call_entry is None:
            place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_BUY)
            call_entry = ce_df.iloc[-2]["close"]
            exec_time  = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
            print(f"[BUY - CE] {CALL_SYMBOL} | Qty: {QTY} | Price: ₹{call_entry} | Time: {exec_time}")
            print(">> CE LEG ENTERED\n")

        if pe_signal and put_entry is None:
            place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_BUY)
            put_entry = pe_df.iloc[-2]["close"]
            exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
            print(f"[BUY - PE] {PUT_SYMBOL} | Qty: {QTY} | Price: ₹{put_entry} | Time: {exec_time}")
            print(">> PE LEG ENTERED\n")

        # ---- TP/SL inner loop ----
        while True:
            if not is_market_open():
                break

            exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")

            # ---- EOD square-off ----
            if is_eod():
                if call_entry is not None:
                    eod_ltp = kite.ltp(f"BFO:{CALL_SYMBOL}")[f"BFO:{CALL_SYMBOL}"]["last_price"]
                    place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                    pnl = (eod_ltp - call_entry) * QTY
                    total_pnl += pnl
                    print(f"[SELL - EOD] {CALL_SYMBOL} | Price: ₹{eod_ltp} | PnL: ₹{pnl:.2f} | Time: {exec_time}")
                    print(">> CE LEG EXITED (EOD)\n")
                    call_entry = None
                if put_entry is not None:
                    eod_ltp = kite.ltp(f"BFO:{PUT_SYMBOL}")[f"BFO:{PUT_SYMBOL}"]["last_price"]
                    place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                    pnl = (eod_ltp - put_entry) * QTY
                    total_pnl += pnl
                    print(f"[SELL - EOD] {PUT_SYMBOL} | Price: ₹{eod_ltp} | PnL: ₹{pnl:.2f} | Time: {exec_time}")
                    print(">> PE LEG EXITED (EOD)\n")
                    put_entry = None
                print(f"── Total PnL for today: ₹{total_pnl:.2f} ──\n")
                break

            # ---- CE TP/SL ----
            if call_entry is not None:
                call_ltp = kite.ltp(f"BFO:{CALL_SYMBOL}")[f"BFO:{CALL_SYMBOL}"]["last_price"]
                if call_ltp >= call_entry + PROFIT_POINTS:
                    place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                    pnl = (call_ltp - call_entry) * QTY
                    total_pnl += pnl
                    print(f"[SELL - TARGET] {CALL_SYMBOL} | Price: ₹{call_ltp} | PnL: ₹{pnl:.2f} | Time: {exec_time}")
                    print(">> CE LEG EXITED (Target hit)\n")
                    call_entry = None
                elif call_ltp <= call_entry - STOPLOSS_POINTS:
                    place_order(kite, CALL_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                    pnl = (call_ltp - call_entry) * QTY
                    total_pnl += pnl
                    print(f"[SELL - STOPLOSS] {CALL_SYMBOL} | Price: ₹{call_ltp} | PnL: ₹{pnl:.2f} | Time: {exec_time}")
                    print(">> CE LEG EXITED (Stoploss hit)\n")
                    call_entry = None

            # ---- PE TP/SL ----
            if put_entry is not None:
                put_ltp = kite.ltp(f"BFO:{PUT_SYMBOL}")[f"BFO:{PUT_SYMBOL}"]["last_price"]
                if put_ltp >= put_entry + PROFIT_POINTS:
                    place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                    pnl = (put_ltp - put_entry) * QTY
                    total_pnl += pnl
                    print(f"[SELL - TARGET] {PUT_SYMBOL} | Price: ₹{put_ltp} | PnL: ₹{pnl:.2f} | Time: {exec_time}")
                    print(">> PE LEG EXITED (Target hit)\n")
                    put_entry = None
                elif put_ltp <= put_entry - STOPLOSS_POINTS:
                    place_order(kite, PUT_SYMBOL, QTY, kite.TRANSACTION_TYPE_SELL)
                    pnl = (put_ltp - put_entry) * QTY
                    total_pnl += pnl
                    print(f"[SELL - STOPLOSS] {PUT_SYMBOL} | Price: ₹{put_ltp} | PnL: ₹{pnl:.2f} | Time: {exec_time}")
                    print(">> PE LEG EXITED (Stoploss hit)\n")
                    put_entry = None

            # ---- Check if new candle formed ----
            ce_candles_check = get_candles_zk(kite, CE_TOKEN, ZK_TF)
            if ce_candles_check:
                new_time = pd.DataFrame(ce_candles_check).iloc[-1]["date"]
                if new_time != last_seen_candle_time:
                    break

            time.sleep(2)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)