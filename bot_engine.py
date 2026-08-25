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
    leg_loss_per_unit,
    should_exit,
)
from utils import resolve_multi_leg_symbols

IST = pytz.timezone("Asia/Kolkata")
SELL_LOT_MULTIPLIER = 3  # kept in sync with inputs.py
ORDER_RETRY_COUNT = 3
ORDER_STATUS_TIMEOUT = 5
POSITION_VERIFY_RETRIES = 4  # Zerodha's aggregate positions feed can lag
POSITION_VERIFY_DELAY = 3    # a few seconds behind the order book after a burst of fills

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
          dry_run                                         (optional, defaults to env_dry_run())
        """
        self.trailing_stop_enabled = config.get("trailing_stop_enabled",False)
        self.trail_amount = config.get("trail_amount",50)
        self.target_profit = config.get("target_profit")

        # Staged partial profit booking: exit 1/3 of each leg's lots at
        # each of 3 thresholds, expressed as a fraction of the net credit
        # collected at entry (the "max profit" reference, fixed once
        # entry completes -- see _compute_max_theoretical_profit).
        self.staged_exit_enabled = config.get("staged_exit_enabled", False)
        self.staged_exit_pcts = config.get("staged_exit_pcts") or [0.5, 0.7, 1.0]
        self.max_theoretical_profit = None
        self.staged_exit_stage = 0        # 0..3, how many stages have fired
        self.staged_lot_plan = {}         # leg -> [stage0_qty, stage1_qty, stage2_qty]

        # Profit Table mode: an alternate, separate profit-booking strategy
        # that only applies to a plain 2-leg short strangle (Sell CE +
        # Sell PE, equal lots, no hedge legs). Independent of the staged
        # exit above -- these two modes are mutually exclusive at the API
        # layer, never both active for the same session.
        #
        # Mechanics (see _run's entry-completion block for the full
        # derivation): Max Profit = (CE premium + PE premium) x lot_size x
        # total_lots. Initial SL ("Running TSL") = Max Profit x the 2nd
        # threshold's percentage. As profit runs, Running TSL trails 1:1
        # with it (Running TSL = stage's base TSL - profit gained since
        # that stage started). Each time screen profit organically reaches
        # a threshold, that stage's lots are booked and the TSL steps down
        # by the theoretical (not actual-fill) profit for that stage. If
        # Running TSL is ever touched first, the whole remaining position
        # exits immediately -- a negative Running TSL means that exit
        # still locks in profit, not a loss.
        self.profit_table_enabled = config.get("profit_table_enabled", False)
        self.profit_table_pcts = config.get("profit_table_pcts") or [0.5, 0.75, 1.0]
        self.pt_stage1_lots_cfg = config.get("profit_table_stage1_lots")
        self.pt_stage2_lots_cfg = config.get("profit_table_stage2_lots")

        self.pt_total_lots = None
        self.pt_entry_prices = {}
        self.pt_original_qty = None
        self.pt_max_profit = None
        self.pt_desired_profit = None
        self.pt_running_tsl = None
        self.pt_tsl_base = None
        self.pt_stage_base_profit = 0.0
        self.pt_stage = 0
        self.pt_stage_lots = []

        self.leg_info = {}
        self.active_legs = []
        self.realized_pnl = 0.0
        self.kite = kite
        self.config = config
        self.index_ltp = None
        self.dry_run = config.get("dry_run")
        if self.dry_run is None:
            self.dry_run = env_dry_run()

        self.per_leg_stop_loss = config.get("per_leg_stop_loss")
        self.per_leg_target = config.get("per_leg_target")

        self._lock = threading.Lock()
        self._manual_stop = threading.Event()
        self._thread = None

        self._stop_only = False
        self._exit_requested = False

        self.status = "idle"
        self.error_message = None
        self.legs = {}
        self.exchange = None
        self.qtys = {}
        self.entry_prices = {}
        self.current_prices = {}
        self.combined_loss = 0.0
        self.max_loss = config.get("max_loss")
        self.exit_reason = None
        self.total_pnl = 0.0
        self.leg_exit_flags = {}
        self.started_at = None
        self.ended_at = None

    # ---------------- public control surface ----------------

    def start(self):
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Bot is already running.")
        self.realized_pnl = 0.0
        self.leg_info = {}
        self.active_legs = []
        self.max_theoretical_profit = None
        self.staged_exit_stage = 0
        self.staged_lot_plan = {}
        self.pt_total_lots = None
        self.pt_entry_prices = {}
        self.pt_original_qty = None
        self.pt_max_profit = None
        self.pt_desired_profit = None
        self.pt_running_tsl = None
        self.pt_tsl_base = None
        self.pt_stage_base_profit = 0.0
        self.pt_stage = 0
        self.pt_stage_lots = []
        self._manual_stop.clear()
        self._stop_only = False
        self._exit_requested = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def request_stop(self):
        """
        Stop monitoring only.
        Leaves all positions open.
        """
        self._stop_only = True
        self._manual_stop.set()

    def request_exit(self):
        """
         Stop monitoring and exit all active positions.
        """
        self._exit_requested = True
        self._manual_stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "index": self.config.get("index"),
                "index_ltp": self.index_ltp,
                "expiry": self.config.get("expiry"),
                "error_message": self.error_message,
                "dry_run": self.dry_run,
                "exchange": self.exchange,
                "legs": self.legs,
                "active_legs": self.active_legs,
                "qtys": self.qtys,
                "entry_prices": self.entry_prices,
                "current_prices": self.current_prices,
                "combined_loss": round(self.combined_loss, 2),
                "max_loss": self.max_loss,
                "exit_reason": self.exit_reason,
                "total_pnl": round(self.total_pnl, 2),
                "leg_exit_flags": self.leg_exit_flags,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "trailing_stop_enabled": self.trailing_stop_enabled,
                "trail_amount": self.trail_amount,
                "target_profit": self.target_profit,
                "staged_exit_enabled": self.staged_exit_enabled,
                "staged_exit_stage": self.staged_exit_stage,
                "max_theoretical_profit": round(self.max_theoretical_profit, 2) if self.max_theoretical_profit is not None else None,
                "profit_table_enabled": self.profit_table_enabled,
                "pt_stage": self.pt_stage,
                "pt_max_profit": round(self.pt_max_profit, 2) if self.pt_max_profit is not None else None,
                "pt_desired_profit": round(self.pt_desired_profit, 2) if self.pt_desired_profit is not None else None,
                "pt_running_tsl": round(self.pt_running_tsl, 2) if self.pt_running_tsl is not None else None,
            }

    def _sync_manual_position_changes(
        self,
        active_legs,
        legs,
        entry_prices,
        current_prices,
        qtys,
        leg_exit_flags,
    ):
        """
        Synchronize the bot with the actual open positions in Kite.

        If the user manually closes any leg from Kite, remove that leg from
        the bot so monitoring, P&L and exit logic stay correct.
        """

        if self.dry_run:
            return active_legs

        positions = self.kite.positions()["net"]

        open_symbols = {
            p["tradingsymbol"]
            for p in positions
            if (
                p["exchange"] == self.exchange
                and p["product"] == self.kite.PRODUCT_NRML
                and p["quantity"] != 0
            )
        }
        updated_active_legs = []

        for leg in active_legs:

            symbol = legs[leg]["symbol"]

            if symbol in open_symbols:
                updated_active_legs.append(leg)

            else:

                print(f"[MANUAL CLOSE DETECTED] {symbol}")

                info = self.leg_info[leg]

                exit_side = (
                    self.kite.TRANSACTION_TYPE_SELL
                    if info["direction"] == "BUY"
                    else self.kite.TRANSACTION_TYPE_BUY
                )

                exit_price = self._find_manual_exit_trade(
                    symbol,
                    exit_side,
                    info["entry_time"],
                )

                if exit_price is not None:

                    if info["direction"] == "BUY":
                        pnl = (exit_price - info["entry_price"]) * info["qty"]
                    else:
                        pnl = (info["entry_price"] - exit_price) * info["qty"]

                    info["realized"] = True
                    info["realized_pnl"] = pnl
                    self.realized_pnl += pnl

                    print(f"Realized P&L = {pnl:.2f}")
                    print(
                        f"{symbol} manually closed."
                        f" Realized P&L = {pnl:.2f}"
                    )

                entry_prices.pop(leg, None)
                qtys.pop(leg, None)
                current_prices.pop(leg, None)
                leg_exit_flags.pop(leg, None)
                self.leg_info.pop(leg, None)

        return updated_active_legs
    
    def _find_manual_exit_trade(
        self,
        symbol,
        transaction_type,
        entry_time,
    ):
        """
        entry_time is a timezone-aware datetime (from datetime.now(IST)).
        Kite's trade fill_timestamp can come back as a naive datetime or a
        string depending on kiteconnect version, which would raise "can't
        compare offset-naive and offset-aware datetimes" against entry_time
        -- so both sides are normalized to naive IST before comparing.
        """

        def _to_naive(ts):
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            if ts.tzinfo is not None:
                ts = ts.astimezone(IST).replace(tzinfo=None)
            return ts

        entry_time_naive = _to_naive(entry_time)

        trades = self.kite.trades()

        candidates = []

        for trade in trades:

            if trade["tradingsymbol"] != symbol:
                continue

            if trade["transaction_type"] != transaction_type:
                continue

            try:
                trade_time = _to_naive(trade["fill_timestamp"])
            except Exception:
                continue

            if trade_time <= entry_time_naive:
                continue

            candidates.append(trade)

        if not candidates:
            return None

        qty = sum(t["quantity"] for t in candidates)

        value = sum(
            t["quantity"] * float(t["average_price"])
            for t in candidates
        )

        return value / qty
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
                product=self.kite.PRODUCT_NRML,
                order_type=self.kite.ORDER_TYPE_MARKET,
                market_protection=self.kite.MARKET_PROTECTION_AUTO,

            )
            print(f"[ORDER PLACED] {action} {qty} x {symbol} | Order ID: {order_id} | Time: {exec_time}")
            return order_id
        except Exception as e:
            import traceback

            error = f"{type(e).__name__}: {e}"

            print("=" * 80)
            print(f"[ORDER FAILED] {action} {qty} x {symbol}")
            print(error)
            traceback.print_exc()
            print("=" * 80)

            self._set(error_message=error)

            return None
    
    def _get_ltp(self, symbol):
        key = f"{self.exchange}:{symbol}"
        return self.kite.ltp(key)[key]["last_price"]

    def _wait_for_order_completion(self, order_id, timeout=ORDER_STATUS_TIMEOUT, dry_run_symbol=None):
        """Wait until Zerodha reports the order as COMPLETE.
        Returns the average fill price (float) if filled, False if
        rejected/cancelled/timeout.

        In dry run there's no real order to poll, so this fetches a live
        LTP for dry_run_symbol when given, to keep paper P&L realistic
        instead of a fixed placeholder; falls back to 100.0 only if that
        LTP fetch itself fails."""
        if self.dry_run:
            if dry_run_symbol:
                try:
                    return self._get_ltp(dry_run_symbol)
                except Exception:
                    pass
            return 100.0
        start = time.time()
        while time.time() - start < timeout:
            try:
                history = self.kite.order_history(order_id)
                last = history[-1]
                status = last["status"]
                if status == "COMPLETE":
                    return float(last["average_price"])
                if status in ("REJECTED", "CANCELLED"):
                    print(f"[ORDER FAILED] {order_id}: {status}")
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

            if self._wait_for_order_completion(order_id, dry_run_symbol=symbol) is not False:
                print(f"{symbol} exited successfully.")
                return True

            print(f"Retrying {symbol}...")

        print(f"Failed to exit {symbol}.")
        return False

    def _exit_partial_leg(self, symbol, qty, transaction_type, retries=ORDER_RETRY_COUNT):
        """
        Same retry logic as _exit_leg, but returns the average fill price
        (or False on failure) instead of a bare bool -- staged profit
        booking needs the fill price to compute realized P&L for the
        exited chunk.
        """

        for attempt in range(1, retries + 1):

            print(f"Partial-exiting {qty} x {symbol} (Attempt {attempt}/{retries})")

            order_id = self._place_order(symbol, qty, transaction_type)

            if not order_id:
                continue

            fill_price = self._wait_for_order_completion(order_id, dry_run_symbol=symbol)

            if fill_price is not False:
                print(f"{qty} x {symbol} partially exited at {fill_price}.")
                return fill_price

            print(f"Retrying partial exit of {symbol}...")

        print(f"Failed to partially exit {qty} x {symbol}.")
        return False

    def _execute_staged_exit(self, active_legs, legs, qtys, entry_prices, exit_txn, leg_exit_flags):
        """
        Books 1/3 of each active leg's original lots at the current
        staged_exit_stage. Mutates qtys/entry_prices in place (reducing
        remaining qty) and folds realized P&L into self.realized_pnl,
        same pattern as manual partial closes. Legs that reach 0 qty are
        dropped from active_legs. Returns the updated active_legs list.
        """
        stage = self.staged_exit_stage
        updated_active_legs = list(active_legs)

        for leg in list(active_legs):
            plan = self.staged_lot_plan.get(leg)
            if not plan:
                continue
            stage_qty = plan[stage]
            if stage_qty <= 0:
                continue  # this leg had too few lots to have anything left at this stage
            stage_qty = min(stage_qty, qtys.get(leg, 0))
            if stage_qty <= 0:
                continue

            symbol = legs[leg]["symbol"]
            fill_price = self._exit_partial_leg(symbol, stage_qty, exit_txn[leg])

            if fill_price is False:
                self._add_log(
                    f"Staged exit stage {stage + 1}: failed to book {stage_qty} x {symbol} -- left open, will retry next tick.",
                    "ERROR",
                )
                continue

            direction = LEG_DIRECTIONS[leg]
            profit = -leg_loss_per_unit(direction, entry_prices[leg], fill_price) * stage_qty
            self.realized_pnl += profit
            qtys[leg] -= stage_qty

            self._add_log(
                f"Staged exit stage {stage + 1}: booked {stage_qty} x {symbol} @ \u20b9{fill_price:.2f} "
                f"(P&L \u20b9{profit:.2f})"
            )

            if qtys[leg] <= 0:
                qtys.pop(leg, None)
                entry_prices.pop(leg, None)
                leg_exit_flags.pop(leg, None)
                self.leg_info.pop(leg, None)
                if leg in updated_active_legs:
                    updated_active_legs.remove(leg)

        self.staged_exit_stage += 1
        return updated_active_legs

    def _execute_profit_table_stage(self, active_legs, legs, qtys, entry_prices, exit_txn, lots_to_exit):
        """
        Books `lots_to_exit` lots from EACH of Sell CE / Sell PE (Profit
        Table mode is 2-leg-only, so this always operates on exactly
        those two symbols). Uses the actual fill price for real realized
        P&L; the Running TSL step-down uses a separate theoretical figure
        computed by the caller, per the spec.
        """
        lot_size = self.config["lot_size"]
        qty_to_exit = lots_to_exit * lot_size
        updated_active_legs = list(active_legs)

        if qty_to_exit <= 0:
            return updated_active_legs

        for leg in ("SELL_CE", "SELL_PE"):
            if leg not in active_legs:
                continue

            leg_qty = min(qty_to_exit, qtys.get(leg, 0))
            if leg_qty <= 0:
                continue

            symbol = legs[leg]["symbol"]
            fill_price = self._exit_partial_leg(symbol, leg_qty, exit_txn[leg])

            if fill_price is False:
                self._add_log(
                    f"Profit Table: failed to book {leg_qty} x {symbol} -- left open, will retry next tick.",
                    "ERROR",
                )
                continue

            profit = (entry_prices[leg] - fill_price) * leg_qty  # SELL leg
            self.realized_pnl += profit
            qtys[leg] -= leg_qty

            self._add_log(
                f"Profit Table booking: {symbol} closed {lots_to_exit} lot(s) @ \u20b9{fill_price:.2f} "
                f"(P&L \u20b9{profit:.2f})"
            )

            if qtys[leg] <= 0:
                qtys.pop(leg, None)
                entry_prices.pop(leg, None)
                self.leg_info.pop(leg, None)
                if leg in updated_active_legs:
                    updated_active_legs.remove(leg)

        return updated_active_legs

    def _profit_table_final_verify(self, symbols_by_leg):
        """
        Per spec: after Profit Table's final exit, wait an extra minute
        and recheck Zerodha's positions directly. If anything is still
        open (a lagged fill, a partial reject that slipped through),
        square it off immediately rather than leaving it unmonitored --
        the session has already ended by this point.
        """
        if self.dry_run:
            return

        time.sleep(60)

        try:
            positions = self.kite.positions()["net"]
        except Exception as e:
            self._add_log(f"Profit Table final recheck: could not fetch positions ({e}).", "ERROR")
            return

        for leg, symbol in symbols_by_leg.items():
            for p in positions:
                if p["tradingsymbol"] != symbol or p["exchange"] != self.exchange or p["quantity"] == 0:
                    continue

                qty_to_close = abs(p["quantity"])
                # Both legs in Profit Table mode are always SELL (short) --
                # closing a residual short position always means buying back.
                success = self._exit_leg(symbol, qty_to_close, self.kite.TRANSACTION_TYPE_BUY, retries=ORDER_RETRY_COUNT)

                if success:
                    self._add_log(f"Profit Table final recheck: squared off {qty_to_close} x {symbol} still open.")
                else:
                    self._add_log(
                        f"Profit Table final recheck: FAILED to square off {qty_to_close} x {symbol} -- MANUAL ACTION REQUIRED.",
                        "ERROR",
                    )


    def _verify_all_positions_closed(self, symbols=None):
        """
        Final safety check -- only looks at the tradingsymbols THIS session
        actually traded. Other positions already open in the account (a
        different strategy, a manual trade, an overnight NRML carry) are
        none of this session's business and shouldn't flag a false error.
        """

        if self.dry_run:
            return True

        symbols = set(symbols or [])

        try:
            positions = self.kite.positions()["net"]
            print("\n========== POSITIONS FROM API ==========")

            open_positions = []

            for p in positions:
                if symbols and p["tradingsymbol"] not in symbols:
                    continue  # not one of this session's legs -- ignore

                print(
                    f"{p['tradingsymbol']} | "
                    f"qty={p['quantity']} | "
                    f"day_buy={p['day_buy_quantity']} | "
                    f"day_sell={p['day_sell_quantity']} |"
                    f"product={p['product']} | "
                    f"exchange={p['exchange']}"
                )

                if p["quantity"] != 0:
                    open_positions.append(p)
            print("========================================")


            if open_positions:
                print("\n========== OPEN POSITIONS ==========")

                for p in open_positions:
                    print(
                        f"{p['tradingsymbol']} "
                        f"Qty={p['quantity']}"
                    )

                print("=============================\n")

                return False
            print("All positions are closed (for this session's legs).\n")
            return True

        except Exception as e:
            print(f"Unable to verify positions: {e}")
            return False

    def _run(self):
        cfg = self.config
        active_legs = []

        if cfg["buy_ce_strike"] is not None:
            active_legs.append("BUY_CE")

        if cfg["buy_pe_strike"] is not None:
            active_legs.append("BUY_PE")

        if cfg["sell_ce_strike"] is not None:
            active_legs.append("SELL_CE")

        if cfg["sell_pe_strike"] is not None:
            active_legs.append("SELL_PE")
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

        lot_size = cfg["lot_size"]
        qtys = {}

        if "BUY_CE" in legs:
            qtys["BUY_CE"] = cfg["buy_ce_lots"] * lot_size

        if "BUY_PE" in legs:
            qtys["BUY_PE"] = cfg["buy_pe_lots"] * lot_size

        if "SELL_CE" in legs:
            qtys["SELL_CE"] = cfg["sell_ce_lots"] * lot_size

        if "SELL_PE" in legs:
            qtys["SELL_PE"] = cfg["sell_pe_lots"] * lot_size
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

                if self._stop_only:
                    self._set(
                        status="stopped",
                        exit_reason="ALGORITHM STOPPED (before entry)",
                        ended_at=datetime.now(IST).isoformat()
                )
                elif self._exit_requested:
                    self._set(
                        status="exited",
                        exit_reason="MANUAL EXIT (before entry)",
                        ended_at=datetime.now(IST).isoformat()
                    )

                return
            time.sleep(30)

        # ---------------- Entry: all 4 legs ----------------
        self._set(status="entering")
        entry_prices = {}
        entered_legs = []
        for leg in active_legs:

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
                    error_message=f"{leg} failed: {self.error_message}",
                    ended_at=datetime.now(IST).isoformat()
                    )
                return

            avg_price = self._wait_for_order_completion(order_id, dry_run_symbol=symbol)

            if avg_price is False:

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
                    error_message=f"{leg} failed: {self.error_message}",
                    ended_at=datetime.now(IST).isoformat()
                )

                return

            entered_legs.append(leg)
            entry_prices[leg] = avg_price
            self.leg_info[leg] = {
                "symbol": symbol,
                "entry_price": avg_price,
                "qty": qtys[leg],
                "direction": LEG_DIRECTIONS[leg],
                "entry_time": datetime.now(IST),
                "realized": False,
                "realized_pnl": 0.0,
            }

        if self.staged_exit_enabled:
            # Net credit collected at entry (SELL premiums received minus
            # BUY premiums paid) -- the only fixed reference available
            # right after entry, used as the "max profit" denominator for
            # the 3 staged thresholds.
            self.max_theoretical_profit = sum(
                (entry_prices[leg] if LEG_DIRECTIONS[leg] == "SELL" else -entry_prices[leg]) * qtys[leg]
                for leg in active_legs
            )
            self._add_log(
                f"Staged profit booking armed. Net credit at entry: \u20b9{self.max_theoretical_profit:.2f} "
                f"(stages at {int(self.staged_exit_pcts[0]*100)}% / "
                f"{int(self.staged_exit_pcts[1]*100)}% / "
                f"{int(self.staged_exit_pcts[2]*100)}%)"
            )
            for leg in active_legs:
                leg_lots = qtys[leg] // lot_size
                stage1_lots = leg_lots // 3
                stage2_lots = leg_lots // 3
                stage3_lots = leg_lots - stage1_lots - stage2_lots
                self.staged_lot_plan[leg] = [
                    stage1_lots * lot_size,
                    stage2_lots * lot_size,
                    stage3_lots * lot_size,
                ]

        if self.profit_table_enabled:
            if set(active_legs) != {"SELL_CE", "SELL_PE"}:
                error = "Profit Table mode requires exactly Sell CE + Sell PE (no other legs)."
                self._set(status="error", error_message=error, ended_at=datetime.now(IST).isoformat())
                self._add_log(error, "ERROR")
                return
            if qtys["SELL_CE"] != qtys["SELL_PE"]:
                error = "Profit Table mode requires equal lots on Sell CE and Sell PE."
                self._set(status="error", error_message=error, ended_at=datetime.now(IST).isoformat())
                self._add_log(error, "ERROR")
                return

            pt_total_lots = qtys["SELL_CE"] // lot_size
            self.pt_total_lots = pt_total_lots
            self.pt_entry_prices = dict(entry_prices)
            self.pt_original_qty = qtys["SELL_CE"]

            # Max profit for a plain short strangle: both legs expiring
            # worthless, i.e. the full combined premium collected.
            self.pt_max_profit = (entry_prices["SELL_CE"] + entry_prices["SELL_PE"]) * self.pt_original_qty
            # Initial SL = Max Profit x the 2nd threshold's percentage --
            # matches the spec exactly (e.g. 75% in the worked example).
            self.pt_desired_profit = self.pt_max_profit * self.profit_table_pcts[1]
            self.pt_running_tsl = self.pt_desired_profit
            self.pt_tsl_base = self.pt_desired_profit
            self.pt_stage_base_profit = 0.0
            self.pt_stage = 0

            s1 = self.pt_stage1_lots_cfg if self.pt_stage1_lots_cfg else pt_total_lots // 3
            s2 = self.pt_stage2_lots_cfg if self.pt_stage2_lots_cfg else pt_total_lots // 3
            s1 = min(s1, pt_total_lots)
            s2 = min(s2, pt_total_lots - s1)
            s3 = pt_total_lots - s1 - s2
            self.pt_stage_lots = [s1, s2, s3]

            self._add_log(
                f"Profit Table armed. Max profit \u20b9{self.pt_max_profit:.0f}, "
                f"Initial SL \u20b9{self.pt_desired_profit:.0f} "
                f"(= Max Profit \u00d7 {int(self.profit_table_pcts[1]*100)}%). "
                f"Stages {int(self.profit_table_pcts[0]*100)}%/{int(self.profit_table_pcts[1]*100)}%/"
                f"{int(self.profit_table_pcts[2]*100)}% \u2192 lots {s1}/{s2}/{s3} of {pt_total_lots} total."
            )

        leg_exit_flags = {}
        self._set(
            status="monitoring",
            legs=legs,
            qtys=qtys,
            entry_prices=entry_prices,
            current_prices=dict(entry_prices),
            leg_exit_flags=leg_exit_flags,
        )
        self.active_legs = active_legs.copy()
        # ---------------- Monitor ----------------
        last_known_prices = dict(entry_prices)

        peak_profit = 0.0
        dynamic_max_loss = cfg["max_loss"]

        while True:
            exit_reason = None

            if self._manual_stop.is_set():

                if self._stop_only:
                    self.active_legs = []
                    self._set(
                        status="stopped",
                        exit_reason="ALGORITHM STOPPED",
                        ended_at=datetime.now(IST).isoformat()
                    )

                    print("Algorithm stopped. Positions remain open.")
                    return

                elif self._exit_requested:
                    exit_reason = "MANUAL EXIT"

            elif not self._is_market_open():
                time.sleep(60)
                continue

            else:
                try:
                    previous_entry_prices = dict(entry_prices)
                    previous_prices = dict(last_known_prices)
                    previous_qtys = dict(qtys)
                    active_legs = self._sync_manual_position_changes(
                        active_legs,
                        legs,
                        entry_prices,
                        last_known_prices,
                        qtys,
                        leg_exit_flags,
                    )
                    self.active_legs = active_legs.copy()
                    if not active_legs:
                        total_pnl = (-compute_combined_loss(
                        previous_entry_prices,
                        previous_prices,
                        previous_qtys,
                        )+self.realized_pnl)

                        self.active_legs = []

                        self._set(
                            status="exited",
                            exit_reason="ALL POSITIONS CLOSED MANUALLY",
                            total_pnl=total_pnl,
                            ended_at=datetime.now(IST).isoformat()
                        )
                        print("All positions were manually closed.")

                        return

                    symbols = [
                        f"{exchange}:{legs[leg]['symbol']}"
                        for leg in active_legs
                    ]
                    # Add underlying index
                    if self.config["index"] == "SENSEX":
                        index_symbol = "BSE:SENSEX"
                    else:
                        index_symbol = "NSE:NIFTY 50"

                    symbols.append(index_symbol)
                    ltp_data = self.kite.ltp(symbols)
                    self.index_ltp = ltp_data[index_symbol]["last_price"]
                    current_prices = {}

                    for leg in active_legs:

                        key = f"{exchange}:{legs[leg]['symbol']}"

                        current_prices[leg] = ltp_data[key]["last_price"]

                    last_known_prices = current_prices
                except Exception as e:
                    self._set(error_message=f"Price fetch error: {e}")
                    time.sleep(10)
                    continue

                combined_loss = compute_combined_loss(entry_prices, current_prices, qtys)
                total_pnl = -combined_loss + self.realized_pnl

                if (self.staged_exit_enabled and active_legs
                        and self.max_theoretical_profit is not None
                        and self.max_theoretical_profit > 0
                        and self.staged_exit_stage < 3):
                    stage_threshold = self.max_theoretical_profit * self.staged_exit_pcts[self.staged_exit_stage]
                    if total_pnl >= stage_threshold:
                        stage_num = self.staged_exit_stage + 1
                        print(f"\n===== STAGED EXIT: stage {stage_num} threshold hit (\u20b9{stage_threshold:.2f}) =====")
                        active_legs = self._execute_staged_exit(
                            active_legs, legs, qtys, entry_prices, exit_txn, leg_exit_flags
                        )
                        self.active_legs = active_legs.copy()
                        current_prices = {leg: current_prices[leg] for leg in active_legs if leg in current_prices}
                        if active_legs:
                            combined_loss = compute_combined_loss(entry_prices, current_prices, qtys)
                            total_pnl = -combined_loss + self.realized_pnl
                        else:
                            combined_loss = 0.0
                            total_pnl = self.realized_pnl
                            self._set(
                                status="exited",
                                exit_reason="STAGED EXIT COMPLETE",
                                total_pnl=total_pnl,
                                combined_loss=combined_loss,
                                ended_at=datetime.now(IST).isoformat()
                            )
                            self._add_log(f"All legs exited via staged profit booking. Final P&L \u20b9{total_pnl:.2f}")
                            print("All legs exited via staged profit booking.")
                            self.active_legs = []
                            return

                if (self.profit_table_enabled and active_legs
                        and self.pt_max_profit is not None
                        and self.pt_max_profit > 0
                        and self.pt_stage < 3):

                    screen_profit = sum(
                        (self.pt_entry_prices[leg] - current_prices[leg]) * self.pt_original_qty
                        for leg in ("SELL_CE", "SELL_PE")
                        if leg in current_prices
                    )

                    delta_since_stage_start = screen_profit - self.pt_stage_base_profit
                    self.pt_running_tsl = self.pt_tsl_base - delta_since_stage_start

                    stage_target = self.pt_max_profit * self.profit_table_pcts[self.pt_stage]
                    tsl_hit = combined_loss >= self.pt_running_tsl
                    target_hit = screen_profit >= stage_target

                    if tsl_hit:
                        lock_desc = (
                            f"locks in \u20b9{-self.pt_running_tsl:.0f} profit"
                            if self.pt_running_tsl < 0
                            else f"caps loss at \u20b9{self.pt_running_tsl:.0f}"
                        )
                        exit_reason = f"PROFIT TABLE TSL HIT ({lock_desc})"

                    elif target_hit and self.pt_stage == 2:
                        exit_reason = "PROFIT TABLE EXIT 3/3 (100% of max profit)"

                    elif target_hit:
                        stage_num = self.pt_stage + 1
                        lots_this_stage = self.pt_stage_lots[self.pt_stage]

                        if lots_this_stage > 0:
                            print(f"\n===== PROFIT TABLE: stage {stage_num} target hit (\u20b9{stage_target:.2f}) =====")
                            active_legs = self._execute_profit_table_stage(
                                active_legs, legs, qtys, entry_prices, exit_txn, lots_this_stage
                            )
                            self.active_legs = active_legs.copy()
                            current_prices = {leg: current_prices[leg] for leg in active_legs if leg in current_prices}

                        theoretical_partial = (
                            stage_target * (lots_this_stage / self.pt_total_lots) if self.pt_total_lots else 0
                        )
                        self.pt_tsl_base = self.pt_running_tsl - theoretical_partial
                        self.pt_stage_base_profit = screen_profit
                        self.pt_stage += 1

                        self._add_log(
                            f"Profit Table stage {stage_num}/3 booked ({lots_this_stage} lot(s) @ "
                            f"{int(self.profit_table_pcts[stage_num-1]*100)}% of max profit). "
                            f"Running TSL now \u20b9{self.pt_tsl_base:.0f}."
                        )

                        if not active_legs:
                            total_pnl = self.realized_pnl
                            self._set(
                                status="exited",
                                exit_reason=f"PROFIT TABLE COMPLETE ({stage_num}/3)",
                                total_pnl=total_pnl,
                                combined_loss=0.0,
                                ended_at=datetime.now(IST).isoformat()
                            )
                            self._add_log(f"All legs exited via Profit Table booking. Final P&L \u20b9{total_pnl:.2f}")
                            print("All legs exited via Profit Table booking.")
                            self.active_legs = []
                            self._profit_table_final_verify({
                                leg: legs[leg]["symbol"] for leg in ("SELL_CE", "SELL_PE") if leg in legs
                            })
                            return

                        combined_loss = compute_combined_loss(entry_prices, current_prices, qtys)
                        total_pnl = -combined_loss + self.realized_pnl

                if (
                    not self.profit_table_enabled
                    and self.target_profit is not None
                    and total_pnl >= self.target_profit
                ):
                    exit_reason = "TARGET PROFIT HIT"
                current_profit = max(total_pnl, 0)
                if self.trailing_stop_enabled:

                    if current_profit > peak_profit:

                        peak_profit = current_profit

                        steps = int(peak_profit // 100)

                        new_dynamic = max(
                            0,
                            cfg["max_loss"] - (steps * self.trail_amount)
                        )

                        if new_dynamic != dynamic_max_loss:
                            dynamic_max_loss = new_dynamic

                            print(
                                f"Trailing SL Updated: ₹{dynamic_max_loss}"
                            )
                # Optional, opt-in per-leg checks. With no per_leg_stop_loss /
                # per_leg_target configured these never fire, so the original
                # "combined loss threshold or manual stop only" behaviour is
                # preserved by default.
                for leg in active_legs:
                    direction = LEG_DIRECTIONS[leg]
                    if leg_hit_stop_loss(direction, entry_prices[leg], current_prices[leg],
                                         qtys[leg], self.per_leg_stop_loss):
                        leg_exit_flags[leg] = "PER-LEG STOP LOSS"
                    elif leg_hit_target(direction, entry_prices[leg], current_prices[leg],
                                        qtys[leg], self.per_leg_target):
                        leg_exit_flags[leg] = "PER-LEG TARGET"

                self._set(current_prices=current_prices,
                           combined_loss=combined_loss,
                           total_pnl=total_pnl,
                           max_loss=dynamic_max_loss,
                            leg_exit_flags=dict(leg_exit_flags),
                            error_message=None,)


                if exit_reason is None:

                    if should_exit(
                        combined_loss,
                        dynamic_max_loss
                    ):
                        exit_reason = "MAX LOSS HIT"

                    elif leg_exit_flags:
                        exit_reason = (
                            f"PER-LEG TRIGGER ({', '.join(leg_exit_flags.values())})"
                        )

            if exit_reason:
                failed_legs = []
                exit_symbols = []

                print(f"\n===== EXIT : {exit_reason} =====")

                for leg in active_legs:
                    symbol = legs[leg]["symbol"]
                    exit_symbols.append(symbol)

                    success = self._exit_leg(
                        symbol,
                        qtys[leg],
                        exit_txn[leg],
                        retries=ORDER_RETRY_COUNT
                    )

                    if not success:
                        failed_legs.append(symbol)

                total_pnl = (-compute_combined_loss(
                    entry_prices,
                    last_known_prices,
                    qtys
                )+self.realized_pnl)

                if failed_legs:
                    message = f"Failed exit legs: {failed_legs}"

                    self._set(
                        status="error",
                        exit_reason=exit_reason,
                        total_pnl=total_pnl,
                        error_message=message,
                        ended_at=datetime.now(IST).isoformat()
                    )

                    print("\n***************")
                    print("MANUAL ACTION REQUIRED")
                    print(message)
                    print("***************\n")
                    self.active_legs = []
                    return

                # Every exit order above already confirmed COMPLETE
                # individually via Zerodha's order book -- the authoritative
                # source for "did this fill". We still cross-check the
                # aggregate positions endpoint as a belt-and-braces sanity
                # net, restricted to this session's own symbols (other
                # positions already open in the account are none of this
                # session's business), and give it a few retries since that
                # feed is known to lag a few seconds behind the order book.
                positions_closed = False
                for attempt in range(1, POSITION_VERIFY_RETRIES + 1):
                    time.sleep(POSITION_VERIFY_DELAY)
                    positions_closed = self._verify_all_positions_closed(symbols=exit_symbols)
                    if positions_closed:
                        break
                    print(f"Rechecking positions... (attempt {attempt}/{POSITION_VERIFY_RETRIES})")

                if not positions_closed:
                    # Don't hard-error here -- every exit order already
                    # confirmed COMPLETE. Surface it as a warning instead so
                    # a transient lag in Zerodha's position feed doesn't
                    # read as a stuck trade when the trade itself is fine.
                    warning = (
                        "All exit orders confirmed COMPLETE, but Zerodha's "
                        "position feed hadn't caught up by the last check -- "
                        "worth a quick manual glance at your Positions tab."
                    )

                    self._set(
                        status="exited",
                        exit_reason=exit_reason,
                        total_pnl=total_pnl,
                        error_message=warning,
                        ended_at=datetime.now(IST).isoformat()
                    )

                    print("\n***************")
                    print("VERIFY MANUALLY (likely just feed lag)")
                    print(warning)
                    print("***************\n")
                    self.active_legs = []
                    return

                self._set(
                    status="exited",
                    exit_reason=exit_reason,
                    total_pnl=total_pnl,
                    ended_at=datetime.now(IST).isoformat()
                )

                print("All positions exited successfully.")
                self.active_legs = []
                if self.profit_table_enabled:
                    self._profit_table_final_verify({
                        leg: legs[leg]["symbol"] for leg in ("SELL_CE", "SELL_PE") if leg in legs
                    })
                return
            
            if self._manual_stop.wait(timeout=2):
                continue