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
ORDER_RETRY_COUNT = 3
ORDER_STATUS_TIMEOUT = 5

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

    def _wait_for_order_completion(self, order_id, timeout=ORDER_STATUS_TIMEOUT):
        """Wait until Zerodha reports the order as COMPLETE.
        Returns True if filled, False if rejected/cancelled/timeout."""

        if self.dry_run:
            return True

        start = time.time()

        while time.time() - start < timeout:
            try:
                orders = self.kite.orders()

                for order in orders:
                    if order["order_id"] == order_id:

                        status = order["status"]

                        if status == "COMPLETE":
                            return True

                        if status in ("REJECTED", "CANCELLED"):
                            print(f"[EXIT FAILED] Order {order_id} : {status}")
                            return False

                time.sleep(1)

            except Exception as e:
                print(f"Error checking order status: {e}")
                time.sleep(1)

        print(f"[TIMEOUT] Order {order_id} not completed.")
        return False


    def _exit_leg(self, symbol, qty, transaction_type,retries=ORDER_RETRY_COUNT):
        """
        Exit one leg with retry logic.
        """

        for attempt in range(1, retries + 1):

            print(f"Exiting {symbol} (Attempt {attempt}/{retries})")

            order_id = self._place_order(symbol, qty, transaction_type)

            if not order_id:
                continue

            if self._wait_for_order_completion(order_id):
                print(f"{symbol} exited successfully.")
                return True

            print(f"Retrying {symbol}...")

        print(f"Failed to exit {symbol}.")
        return False


    def _verify_all_positions_closed(self):
        """
        Final safety check.
        """

        if self.dry_run:
            return True

        try:
            positions = self.kite.positions()["net"]

            open_positions = [
                p for p in positions
                if p["quantity"] != 0
            ]

            if open_positions:
                print("\n========== WARNING ==========")

                for p in open_positions:
                    print(
                        f"{p['tradingsymbol']} "
                        f"Qty={p['quantity']}"
                    )

                print("=============================\n")

                return False

            return True

        except Exception as e:
            print(f"Unable to verify positions: {e}")
            return False

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
        entered_legs = []
        for leg in LEGS:

            symbol = legs[leg]["symbol"]

            order_id = self._place_order(
                symbol,
                qtys[leg],
                entry_txn[leg]
            )

            if not order_id:

                print("\nENTRY FAILED")
                print("Rolling back previously entered positions...")
                rollback_failed = []

                for entered_leg in reversed(entered_legs):

                    success=self._exit_leg(
                        legs[entered_leg]["symbol"],
                        qtys[entered_leg],
                        exit_txn[entered_leg],
                        retries=ORDER_RETRY_COUNT
                    )
                    if not success:
                        rollback_failed.append(legs[entered_leg]["symbol"])

                if rollback_failed:
                    self._set(
                        status="error",
                        error_message=(
                            f"Failed to enter {leg}. "
                            f"Rollback failed for: {rollback_failed}"
                        ),
                        ended_at=datetime.now(IST).isoformat()
                    )
                else:
                    self._set(
                        status="error",
                        error_message=f"Failed to enter {leg}. Previous entries rolled back.",
                        ended_at=datetime.now(IST).isoformat()
                    )
                return

            if not self._wait_for_order_completion(order_id):

                print("\nENTRY INCOMPLETE")
                print("Rolling back previously entered positions...")

                rollback_failed = []

                for entered_leg in reversed(entered_legs):

                    success = self._exit_leg(
                        legs[entered_leg]["symbol"],
                        qtys[entered_leg],
                        exit_txn[entered_leg],
                        retries=ORDER_RETRY_COUNT
                    )

                    if not success:
                        rollback_failed.append(legs[entered_leg]["symbol"])

                if rollback_failed:

                    self._set(
                        status="error",
                        error_message=(
                            f"{leg} entry failed and rollback was incomplete. "
                            f"Open positions may remain: {rollback_failed}"
                        ),
                        ended_at=datetime.now(IST).isoformat()
                    )

                else:

                    self._set(
                        status="error",
                        error_message=f"{leg} entry was not completed. Previous entries rolled back.",
                        ended_at=datetime.now(IST).isoformat()
                    )

                return

            entry_prices[leg] = self._get_ltp(symbol)
            entered_legs.append(leg)
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

                failed_legs = []

                print(f"\n===== EXIT : {exit_reason} =====")

                for leg in LEGS:

                    success = self._exit_leg(
                        legs[leg]["symbol"],
                        qtys[leg],
                        exit_txn[leg],
                        retries=ORDER_RETRY_COUNT
                    )

                    if not success:
                        failed_legs.append(legs[leg]["symbol"])
                time.sleep(2)  # give Zerodha a moment to update positions before final check
                positions_closed = self._verify_all_positions_closed()

                total_pnl = -compute_combined_loss(
                    entry_prices,
                    last_known_prices,
                    qtys
                )

                if failed_legs or not positions_closed:

                    self._set(
                        status="error",
                        exit_reason=exit_reason,
                        total_pnl=total_pnl,
                        error_message=(
                            f"Some positions could not be closed. "
                            f"Failed legs: {failed_legs}"
                        ),
                        ended_at=datetime.now(IST).isoformat()
                    )

                    print("\n***************")
                    print("MANUAL ACTION REQUIRED")
                    print("Check Zerodha positions immediately.")
                    print("***************\n")

                    return

                self._set(
                    status="exited",
                    exit_reason=exit_reason,
                    total_pnl=total_pnl,
                    ended_at=datetime.now(IST).isoformat()
                )

                print("All positions exited successfully.")
                return
            
            if self._manual_stop.wait(timeout=5):
                continue