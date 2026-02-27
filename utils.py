# utils.py
from datetime import datetime
import pandas as pd
import pytz
IST = pytz.timezone("Asia/Kolkata")

def resolve_ce_pe_by_strikes(kite, call_strike, put_strike):
    instruments = pd.DataFrame(kite.instruments("BFO"))

    sensex_opts = instruments[instruments["tradingsymbol"].str.startswith("SENSEX")]

    today = datetime.now(IST).date()
    sensex_opts = sensex_opts[sensex_opts["expiry"] >= today]
    sensex_opts = sensex_opts.sort_values("expiry")
    nearest_expiry = sensex_opts.iloc[0]["expiry"]

    ce_row = sensex_opts[
        (sensex_opts["expiry"] == nearest_expiry) &
        (sensex_opts["strike"] == call_strike) &
        (sensex_opts["instrument_type"] == "CE")
    ]

    pe_row = sensex_opts[
        (sensex_opts["expiry"] == nearest_expiry) &
        (sensex_opts["strike"] == put_strike) &
        (sensex_opts["instrument_type"] == "PE")
    ]

    if ce_row.empty or pe_row.empty:
        raise ValueError("Could not resolve CE/PE for given strikes. Check strikes or expiry.")

    ce_symbol = ce_row.iloc[0]["tradingsymbol"]
    pe_symbol = pe_row.iloc[0]["tradingsymbol"]
    ce_token = int(ce_row.iloc[0]["instrument_token"])
    pe_token = int(pe_row.iloc[0]["instrument_token"])

    return ce_symbol, pe_symbol, ce_token, pe_token, nearest_expiry
