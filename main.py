# main.py
"""
Interactive CLI runner for local/manual testing.

Uses the same TradingBot engine as main_api.py (see bot_engine.py), so
there's no behavioural drift between "run it from the terminal" and
"drive it from the dashboard".
"""
import time

from bot_engine import TradingBot, env_dry_run
from inputs import get_user_inputs
from zerodha_client import get_kite


def main():
    access_token = input("Paste Zerodha ACCESS_TOKEN for today: ").strip()
    kite = get_kite(access_token)
    print("Logged in as:", kite.profile()["user_name"])

    dry_run = env_dry_run()
    if dry_run:
        print("\n  DRY RUN MODE -- No real orders will be placed.")
        print("  (set DRY_RUN=false in your environment/.env to go live)\n")
    else:
        print("\n LIVE MODE -- Real orders will be placed!\n")

    user = get_user_inputs()

    config = {
        "index": user["INDEX"],
        "expiry": user["EXPIRY"],
        "buy_ce_strike": user["BUY_CE_STRIKE"],
        "buy_pe_strike": user["BUY_PE_STRIKE"],
        "sell_ce_strike": user["SELL_CE_STRIKE"],
        "sell_pe_strike": user["SELL_PE_STRIKE"],
        "buy_ce_lots": user["BUY_CE_LOTS"],
        "buy_pe_lots": user["BUY_PE_LOTS"],
        "sell_ce_lots": user["SELL_CE_LOTS"],
        "sell_pe_lots": user["SELL_PE_LOTS"],
        "lot_size": user["LOT_SIZE"],
        "max_loss": user["MAX_LOSS"],
        "dry_run": dry_run,
    }

    bot = TradingBot(kite, config)
    bot.start()

    last_status = None
    try:
        while True:
            snap = bot.snapshot()
            if snap["status"] != last_status:
                print(f"[STATUS] {snap['status']}")
                last_status = snap["status"]
            if snap["status"] in ("exited", "error", "stopped"):
                print(snap)
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nManual exit requested (Ctrl+C). Exiting all active legs...")
        bot.request_exit()
        while True:
            snap = bot.snapshot()
            if snap["status"] in ("exited", "error", "stopped"):
                print(snap)
                break
            time.sleep(2)


if __name__ == "__main__":
    main()