# bot_engine.py
"""
Single source of truth for the 4-leg entry/monitor/exit loop.

Both main.py (interactive CLI) and main_api.py (FastAPI service) build a
TradingBot with a plain config dict and call .start(). This is what
main.py and main_api.py used to duplicate wholesale — now there's exactly
one place that places orders and checks exit conditions.

TradingBot.run() has no input() calls and no reliance on globals, so it's
safe to run on a background thread inside a web server.
"""
import os
import threading
import time
from datetime import datetime

import pytz

from strategy import (
    LEG_DIRECTIONS,
    compute_combined_loss,
    leg_hit_stop_loss,
    leg_hit_target,
    should_exit,
)
from utils import resolve_multi_leg_symbols

IST = pytz.timezone("Asia/Kolkata")
LEGS = ["BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE"]
SELL_LOT_MULTIPLIER = 3  # kept in sync with inputs.py


def env_dry_run() -> bool:
    """DRY_RUN is now an env var (e.g. Heroku config var / .env entry)
    instead of a hardcoded constant, so live trading is a deploy-time
    flag flip, not a code edit. Defaults to True — you have to
    deliberately opt into live orders."""
    return os.getenv("DRY_RUN", "true").strip().lower() not in ("0", "false", "no")


class TradingBot:
    """
    Runs one 4-leg session: resolve contracts -> wait for market open ->
    enter all 4 legs -> monitor -> exit on combined-loss / optional
    per-leg SL / optional per-leg target / EOD square-off / manual stop.
    """

    def __init__(self, kite, config: dict):
        """
        config keys:
          index, expiry, buy_ce_strike, buy_pe_strike, sell_ce_strike,
          sell_pe_strike, buy_lots, lot_size, max_loss  (required)
          per_leg_stop_loss, per_leg_target              (optional, rupees)
          square_off_time                                (optional, "HH:MM" IST, default "15:20")
          dry_run                                         (optional, defaults to env_dry_run())
        """
        self.kite = kite
        self.config = config
        self.dry_run = config.get("dry_run")
        if self.dry_run is None:
            self.dry_run = env_dry_run()

        self.square_off_time = config.get("square_off_time", "15:20")
        self.per_leg_stop_loss = config.get("per_leg_stop_loss")
        self.per_leg_target = config.get("per_leg_target")

        self._lock = threading.Lock()
        self._manual_stop = threading.Event()
        self._thread = None

        self.status = "idle"
        self.error_message = None
        self.legs = {}
        self.exchange = None
        self.qtys = {}
        self.entry_prices = {}
        self.current_prices = {}
        self.combined_loss = 0.0
        self.exit_reason = None
        self.total_pnl = 0.0
        self.leg_exit_flags = {}
        self.started_at = None
        self.ended_at = None

    # ---------------- public control surface ----------------

    def start(self):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Bot is already running.")
        self._manual_stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_stop(self):
        """Signal the monitor loop to exit all legs on its next tick."""
        self._manual_stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "error_message": self.error_message,
                "dry_run": self.dry_run,
                "exchange": self.exchange,
                "legs": self.legs,
                "qtys": self.qtys,
                "entry_prices": self.entry_prices,
                "current_prices": self.current_prices,
                "combined_loss": round(self.combined_loss, 2),
                "max_loss": self.config.get("max_loss"),
                "exit_reason": self.exit_reason,
                "total_pnl": round(self.total_pnl, 2),
                "leg_exit_flags": self.leg_exit_flags,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
            }

    # ---------------- internals ----------------

    def _set(self, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def _is_market_open(self):
        now = datetime.now(IST)
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now <= market_close

    def _is_square_off_time(self):
        now = datetime.now(IST)
        hh, mm = (int(x) for x in self.square_off_time.split(":"))
        return now >= now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    def _get_ltp(self, symbol):
        key = f"{self.exchange}:{symbol}"
        return self.kite.ltp(key)[key]["last_price"]

    def _place_order(self, symbol, qty, transaction_type):
        exec_time = datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S")
        action = "BUY" if transaction_type == self.kite.TRANSACTION_TYPE_BUY else "SELL"

        if self.dry_run:
            print(f"[DRY RUN] {action} {qty} x {symbol} at {exec_time}")
            return "DRY_RUN_ORDER"

        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.exchange,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=qty,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET,
            )
            print(f"[ORDER PLACED] {action} {qty} x {symbol} | Order ID: {order_id} | Time: {exec_time}")
            return order_id
        except Exception as e:
            print(f"[ORDER FAILED] {action} {symbol} | Error: {e}")
            return None

    def _run(self):
        cfg = self.config
        self._set(status="resolving", started_at=datetime.now(IST).isoformat(), error_message=None)

        try:
            legs, exchange = resolve_multi_leg_symbols(
                self.kite, cfg["index"], cfg["expiry"],
                cfg["buy_ce_strike"], cfg["buy_pe_strike"],
                cfg["sell_ce_strike"], cfg["sell_pe_strike"],
            )
        except Exception as e:
            self._set(status="error", error_message=str(e), ended_at=datetime.now(IST).isoformat())
            return

        buy_qty = cfg["buy_lots"] * cfg["lot_size"]
        sell_qty = cfg["buy_lots"] * SELL_LOT_MULTIPLIER * cfg["lot_size"]
        qtys = {"BUY_CE": buy_qty, "BUY_PE": buy_qty, "SELL_CE": sell_qty, "SELL_PE": sell_qty}

        entry_txn = {
            leg: (self.kite.TRANSACTION_TYPE_BUY if d == "BUY" else self.kite.TRANSACTION_TYPE_SELL)
            for leg, d in LEG_DIRECTIONS.items()
        }
        exit_txn = {
            leg: (self.kite.TRANSACTION_TYPE_SELL if d == "BUY" else self.kite.TRANSACTION_TYPE_BUY)
            for leg, d in LEG_DIRECTIONS.items()
        }

        self._set(status="waiting_for_market", legs=legs, exchange=exchange, qtys=qtys)

        while not self._is_market_open():
            if self._manual_stop.is_set():
                self._set(status="exited", exit_reason="MANUAL STOP (before entry)",
                          ended_at=datetime.now(IST).isoformat())
                return
            time.sleep(30)

        # ---------------- Entry: all 4 legs ----------------
        self._set(status="entering")
        entry_prices = {}
        for leg in LEGS:
            symbol = legs[leg]["symbol"]
            self._place_order(symbol, qtys[leg], entry_txn[leg])
            entry_prices[leg] = self._get_ltp(symbol)
        self._set(status="monitoring", entry_prices=entry_prices, current_prices=dict(entry_prices))

        # ---------------- Monitor ----------------
        leg_exit_flags = {}
        last_known_prices = dict(entry_prices)

        while True:
            exit_reason = None

            if self._manual_stop.is_set():
                exit_reason = "MANUAL STOP"
            elif not self._is_market_open():
                time.sleep(60)
                continue
            else:
                try:
                    current_prices = {leg: self._get_ltp(legs[leg]["symbol"]) for leg in LEGS}
                    last_known_prices = current_prices
                except Exception as e:
                    self._set(error_message=f"Price fetch error: {e}")
                    time.sleep(10)
                    continue

                combined_loss = compute_combined_loss(entry_prices, current_prices, qtys)

                # Optional, opt-in per-leg checks. With no per_leg_stop_loss /
                # per_leg_target configured these never fire, so the original
                # "combined loss threshold or manual stop only" behaviour is
                # preserved by default.
                for leg, direction in LEG_DIRECTIONS.items():
                    if leg_hit_stop_loss(direction, entry_prices[leg], current_prices[leg],
                                         qtys[leg], self.per_leg_stop_loss):
                        leg_exit_flags[leg] = "PER-LEG STOP LOSS"
                    elif leg_hit_target(direction, entry_prices[leg], current_prices[leg],
                                        qtys[leg], self.per_leg_target):
                        leg_exit_flags[leg] = "PER-LEG TARGET"

                self._set(current_prices=current_prices, combined_loss=combined_loss,
                          leg_exit_flags=dict(leg_exit_flags))

                if should_exit(combined_loss, cfg["max_loss"]):
                    exit_reason = "MAX LOSS HIT"
                elif leg_exit_flags:
                    exit_reason = f"PER-LEG TRIGGER ({', '.join(leg_exit_flags.values())})"
                elif self._is_square_off_time():
                    exit_reason = "EOD SQUARE-OFF"

            if exit_reason:
                for leg in LEGS:
                    self._place_order(legs[leg]["symbol"], qtys[leg], exit_txn[leg])
                total_pnl = -compute_combined_loss(entry_prices, last_known_prices, qtys)
                self._set(status="exited", exit_reason=exit_reason, total_pnl=total_pnl,
                          ended_at=datetime.now(IST).isoformat())
                return

            time.sleep(5)