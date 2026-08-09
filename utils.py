# utils.py
from datetime import datetime
import threading
import time

import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ---------------- instrument dump cache ----------------
#
# kite.instruments(exchange) returns EVERY contract on that exchange --
# tens of thousands of rows -- and used to get called on every single
# strike lookup here. That's fine once, but the dashboard's live-premium
# polling (every 2s, plus a debounced call on every keystroke) turned
# "once" into "dozens of times a minute", which is what was tripping
# Zerodha's rate limit and surfacing as "Too many requests" in the UI.
#
# The instrument list only changes once a day (new contracts get listed
# at day start), so it's cached per exchange per calendar day instead of
# being re-fetched on every lookup.
_instrument_cache = {}            # exchange -> (date, DataFrame)
_instrument_backoff_until = {}    # exchange -> unix timestamp
_instrument_cache_lock = threading.Lock()

# If a fetch fails (almost always a rate limit), don't retry on the very
# next request -- that just re-triggers the same 429 every poll tick and
# keeps the cooldown alive indefinitely. Back off for a stretch instead.
_INSTRUMENT_BACKOFF_SECONDS = 15


def _get_instruments_df(kite, exchange):
    today = datetime.now(IST).date()
    now = time.time()

    with _instrument_cache_lock:
        cached = _instrument_cache.get(exchange)
        if cached and cached[0] == today:
            return cached[1]

        backoff_until = _instrument_backoff_until.get(exchange, 0)
        if now < backoff_until:
            wait = round(backoff_until - now, 1)
            raise RuntimeError(
                f"Instrument list temporarily unavailable (rate limited). "
                f"Retrying automatically in {wait}s -- not retrying immediately "
                f"to avoid keeping the rate limit tripped."
            )

    # Fetch outside the lock -- it's a slow network call, and blocking
    # every other in-flight request on it (including ones for a different
    # exchange) is worse than the rare case of two threads both fetching
    # once on a cache-cold start.
    try:
        df = pd.DataFrame(kite.instruments(exchange))
    except Exception:
        with _instrument_cache_lock:
            _instrument_backoff_until[exchange] = time.time() + _INSTRUMENT_BACKOFF_SECONDS
        raise

    with _instrument_cache_lock:
        _instrument_cache[exchange] = (today, df)
        _instrument_backoff_until.pop(exchange, None)

    return df


def resolve_ce_pe_by_strikes(kite, call_strike, put_strike):
    instruments = _get_instruments_df(kite, "BFO")

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


def resolve_multi_leg_symbols(kite, index, expiry_str, buy_ce_strike=None, buy_pe_strike=None,
                               sell_ce_strike=None, sell_pe_strike=None):
    """
    Resolve tradingsymbols/tokens for whichever of the 4 legs the user
    actually entered a strike for:
      BUY_CE           @ buy_ce_strike   (skipped if buy_ce_strike is None)
      BUY_PE           @ buy_pe_strike   (skipped if buy_pe_strike is None)
      SELL_CE          @ sell_ce_strike  (skipped if sell_ce_strike is None)
      SELL_PE          @ sell_pe_strike  (skipped if sell_pe_strike is None)
    for an EXACT user-specified expiry (not "nearest") on the correct exchange.
    At least one strike must be given.

    Returns:
      legs: dict[leg_name -> {"symbol": str, "token": int}]  -- only the
            legs the user actually entered a strike for
      exchange: str ("BFO" or "NFO")
    """
    if index not in INDEX_EXCHANGE:
        raise ValueError(f"Unsupported index '{index}'. Must be SENSEX or NIFTY.")

    requested = {
        "BUY_CE":  ("CE", buy_ce_strike),
        "BUY_PE":  ("PE", buy_pe_strike),
        "SELL_CE": ("CE", sell_ce_strike),
        "SELL_PE": ("PE", sell_pe_strike),
    }
    active = {leg: (opt_type, strike) for leg, (opt_type, strike) in requested.items() if strike is not None}

    if not active:
        raise ValueError(
            "No legs entered -- provide a strike for at least one of "
            "buy_ce_strike / buy_pe_strike / sell_ce_strike / sell_pe_strike."
        )

    exchange = INDEX_EXCHANGE[index]

    try:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Expiry '{expiry_str}' is not in YYYY-MM-DD format.")

    instruments = _get_instruments_df(kite, exchange)
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

    legs = {}
    for leg, (opt_type, strike) in active.items():
        row = find_row(strike, opt_type)
        legs[leg] = {"symbol": row["tradingsymbol"], "token": int(row["instrument_token"])}

    return legs, exchange