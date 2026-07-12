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


# Exchange each index's options trade on
INDEX_EXCHANGE = {
    "SENSEX": "BFO",
    "NIFTY":  "NFO",
}


def resolve_multi_leg_symbols(kite, index, expiry_str, buy_ce_strike, buy_pe_strike, sell_ce_strike, sell_pe_strike):
    """
    Resolve tradingsymbols/tokens for the 4-leg structure:
      BUY_CE           @ buy_ce_strike
      BUY_PE           @ buy_pe_strike
      SELL_CE          @ sell_ce_strike
      SELL_PE          @ sell_pe_strike
    for an EXACT user-specified expiry (not "nearest") on the correct exchange.

    Returns:
      legs: dict[leg_name -> {"symbol": str, "token": int}]
      exchange: str ("BFO" or "NFO")
    """
    if index not in INDEX_EXCHANGE:
        raise ValueError(f"Unsupported index '{index}'. Must be SENSEX or NIFTY.")

    exchange = INDEX_EXCHANGE[index]

    try:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Expiry '{expiry_str}' is not in YYYY-MM-DD format.")

    instruments = pd.DataFrame(kite.instruments(exchange))
    opts = instruments[instruments["tradingsymbol"].str.startswith(index)]
    opts = opts[opts["expiry"] == expiry_date]

    if opts.empty:
        raise ValueError(
            f"No {index} instruments found for expiry {expiry_str} on {exchange}. "
            f"Confirm this is a currently-listed expiry."
        )

    def find_row(strike, opt_type):
        row = opts[(opts["strike"] == strike) & (opts["instrument_type"] == opt_type)]
        if row.empty:
            raise ValueError(
                f"Could not resolve {index} {strike} {opt_type} for expiry {expiry_str}."
            )
        return row.iloc[0]

    buy_ce_row  = find_row(buy_ce_strike, "CE")
    buy_pe_row  = find_row(buy_pe_strike, "PE")
    sell_ce_row = find_row(sell_ce_strike, "CE")
    sell_pe_row = find_row(sell_pe_strike, "PE")

    legs = {
        "BUY_CE":  {"symbol": buy_ce_row["tradingsymbol"],  "token": int(buy_ce_row["instrument_token"])},
        "BUY_PE":  {"symbol": buy_pe_row["tradingsymbol"],  "token": int(buy_pe_row["instrument_token"])},
        "SELL_CE": {"symbol": sell_ce_row["tradingsymbol"], "token": int(sell_ce_row["instrument_token"])},
        "SELL_PE": {"symbol": sell_pe_row["tradingsymbol"], "token": int(sell_pe_row["instrument_token"])},
    }

    return legs, exchange