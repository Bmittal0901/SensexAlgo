from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")

def get_option_ltp(kite, symbol):
    q = kite.ltp(f"BFO:{symbol}")
    return q[f"BFO:{symbol}"]["last_price"]

def buy_signal(kite, symbol, qty):
    ltp = get_option_ltp(kite, symbol)
    time_now = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
    print(f"[BUY SIGNAL]  {symbol} | Qty: {qty} | Price: ₹{ltp} | Time: {time_now}")
    return ltp

def sell_signal(kite, symbol, qty, reason, ltp):
    time_now = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
    print(f"[SELL SIGNAL - {reason}]  {symbol} | Qty: {qty} | Price: ₹{ltp} | Time: {time_now}")
    return ltp

