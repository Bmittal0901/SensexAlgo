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


def resolve_multi_leg_symbols(
    kite,
    index,
    expiry_str,
    buy_ce_strike,
    buy_pe_strike,
    sell_ce_strike,
    sell_pe_strike,
):
    """
    Resolve only the legs whose strikes are provided.

    Returns:
        legs: {
            "BUY_CE": {"symbol": ..., "token": ...},
            ...
        }
        exchange
    """

    if index not in INDEX_EXCHANGE:
        raise ValueError(f"Unsupported index '{index}'.")

    exchange = INDEX_EXCHANGE[index]

    try:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Expiry must be YYYY-MM-DD.")

    instruments = pd.DataFrame(kite.instruments(exchange))

    opts = instruments[
        instruments["tradingsymbol"].str.startswith(index)
    ]

    opts = opts[
        opts["expiry"] == expiry_date
    ]

    if opts.empty:
        raise ValueError(
            f"No {index} contracts found for expiry {expiry_str}."
        )

    def find_row(strike, opt_type):

        if strike is None:
            return None

        row = opts[
            (opts["strike"] == strike) &
            (opts["instrument_type"] == opt_type)
        ]

        if row.empty:
            raise ValueError(
                f"Could not resolve {index} {strike} {opt_type}"
            )

        return row.iloc[0]

    legs = {}

    buy_ce = find_row(buy_ce_strike, "CE")
    if buy_ce is not None:
        legs["BUY_CE"] = {
            "symbol": buy_ce["tradingsymbol"],
            "token": int(buy_ce["instrument_token"]),
        }

    buy_pe = find_row(buy_pe_strike, "PE")
    if buy_pe is not None:
        legs["BUY_PE"] = {
            "symbol": buy_pe["tradingsymbol"],
            "token": int(buy_pe["instrument_token"]),
        }

    sell_ce = find_row(sell_ce_strike, "CE")
    if sell_ce is not None:
        legs["SELL_CE"] = {
            "symbol": sell_ce["tradingsymbol"],
            "token": int(sell_ce["instrument_token"]),
        }

    sell_pe = find_row(sell_pe_strike, "PE")
    if sell_pe is not None:
        legs["SELL_PE"] = {
            "symbol": sell_pe["tradingsymbol"],
            "token": int(sell_pe["instrument_token"]),
        }

    if not legs:
        raise ValueError("Please enter at least one strike.")

    return legs, exchange