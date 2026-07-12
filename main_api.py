# main.py
import time
from datetime import datetime
import pytz

from inputs import get_user_inputs
from strategy import LEG_DIRECTIONS, compute_combined_loss, should_exit
from utils import resolve_multi_leg_symbols
from zerodha_client import get_kite

IST = pytz.timezone("Asia/Kolkata")

# SET TO False WHEN READY TO TRADE REAL MONEY
DRY_RUN = True

LEGS = ["BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE"]


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


# ---------------- Order Placement ----------------
def place_order(kite, exchange, symbol, qty, transaction_type):
    exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
    action = "BUY" if transaction_type == kite.TRANSACTION_TYPE_BUY else "SELL"

    if DRY_RUN:
        print(f"[DRY RUN] {action} {qty} x {symbol} at {exec_time}")
        return "DRY_RUN_ORDER"

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

INDEX          = user["INDEX"]
EXPIRY         = user["EXPIRY"]
BUY_CE_STRIKE  = user["BUY_CE_STRIKE"]
BUY_PE_STRIKE  = user["BUY_PE_STRIKE"]
SELL_CE_STRIKE = user["SELL_CE_STRIKE"]
SELL_PE_STRIKE = user["SELL_PE_STRIKE"]
BUY_LOTS       = user["BUY_LOTS"]
SELL_LOTS      = user["SELL_LOTS"]
LOT_SIZE       = user["LOT_SIZE"]
MAX_LOSS       = user["MAX_LOSS"]

QTY_BUY  = BUY_LOTS * LOT_SIZE
QTY_SELL = SELL_LOTS * LOT_SIZE

QTYS = {
    "BUY_CE":  QTY_BUY,
    "BUY_PE":  QTY_BUY,
    "SELL_CE": QTY_SELL,
    "SELL_PE": QTY_SELL,
}

# Entry transaction type per leg (derived from strategy.LEG_DIRECTIONS)
ENTRY_TXN = {
    leg: (kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL)
    for leg, direction in LEG_DIRECTIONS.items()
}
# Exit is always the reverse of entry
EXIT_TXN = {
    leg: (kite.TRANSACTION_TYPE_SELL if direction == "BUY" else kite.TRANSACTION_TYPE_BUY)
    for leg, direction in LEG_DIRECTIONS.items()
}

# ---------------- Resolve contracts ----------------
legs, EXCHANGE = resolve_multi_leg_symbols(
    kite, INDEX, EXPIRY, BUY_CE_STRIKE, BUY_PE_STRIKE, SELL_CE_STRIKE, SELL_PE_STRIKE
)

print("\nResolved contracts:")
for leg in LEGS:
    print(f"  {leg:9s}: {legs[leg]['symbol']}  (qty {QTYS[leg]})")
print("Exchange  :", EXCHANGE)
print("Expiry    :", EXPIRY)
print("Max Loss  : ₹", MAX_LOSS)

# ---------------- Wait for market open ----------------
while not is_market_open():
    print("Market closed. Waiting to enter...")
    time.sleep(30)

# ---------------- Entry: place all 4 legs ----------------
entry_prices = {}
for leg in LEGS:
    symbol = legs[leg]["symbol"]
    place_order(kite, EXCHANGE, symbol, QTYS[leg], ENTRY_TXN[leg])
    entry_prices[leg] = get_ltp(kite, EXCHANGE, symbol)
    exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
    print(f"[{leg}] {symbol} | Qty: {QTYS[leg]} | Entry: ₹{entry_prices[leg]} | Time: {exec_time}")

print(">> ALL 4 LEGS ENTERED\n")

in_trade = True
total_pnl = 0.0

# ---------------- Monitor loop ----------------
while in_trade:
    try:
        if not is_market_open():
            print("Market closed. Sleeping 60s...")
            time.sleep(60)
            continue

        current_prices = {
            leg: get_ltp(kite, EXCHANGE, legs[leg]["symbol"]) for leg in LEGS
        }
        combined_loss = compute_combined_loss(entry_prices, current_prices, QTYS)

        exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
        print(f"[{exec_time}] Combined loss: ₹{combined_loss:.2f} (max: ₹{MAX_LOSS})")

        exit_reason = None
        if should_exit(combined_loss, MAX_LOSS):
            exit_reason = "MAX LOSS HIT"
        elif is_eod():
            exit_reason = "EOD"

        if exit_reason:
            for leg in LEGS:
                symbol = legs[leg]["symbol"]
                place_order(kite, EXCHANGE, symbol, QTYS[leg], EXIT_TXN[leg])
                exit_price = current_prices[leg]
                print(f"[EXIT - {exit_reason}] {leg} {symbol} | Price: ₹{exit_price} | Time: {exec_time}")

            total_pnl = -combined_loss
            print(f"\n── Exit reason: {exit_reason} | Total PnL: ₹{total_pnl:.2f} ──\n")
            in_trade = False
            break

        time.sleep(5)

    except Exception as e:
        print("Error:", e)
        time.sleep(10)

print("Algo finished.")