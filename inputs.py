# inputs.py

LOT_SIZES = {"SENSEX": 20, "NIFTY": 65}  # hardcoded, matches main_api.py


def _ask_leg(label):
    """Returns (strike, lots) or (None, None) if the user leaves it blank
    (i.e. skips this leg entirely)."""
    raw = input(f"  Strike for {label} (blank to skip this leg): ").strip()
    if not raw:
        return None, None
    strike = int(raw)
    lots = int(input(f"    Lots for {label}: ").strip())
    if lots <= 0:
        raise ValueError(f"{label}: lots must be >= 1")
    return strike, lots


def get_user_inputs():
    print("\n=== Multi-Leg Options Algo Setup ===\n")

    index = input("Select index (SENSEX / NIFTY): ").strip().upper()
    if index not in ["SENSEX", "NIFTY"]:
        raise ValueError("Index must be SENSEX or NIFTY")

    expiry = input("Enter expiry date (YYYY-MM-DD, must be a currently-listed expiry): ").strip()
    if not expiry:
        raise ValueError("Expiry is required")

    print(f"\nEnter whichever of the 4 legs you want this session to trade for {index}")
    print("(leave a strike blank to skip that leg -- 1 to 4 legs are all fine):\n")

    buy_ce_strike, buy_ce_lots = _ask_leg("BUY CE")
    buy_pe_strike, buy_pe_lots = _ask_leg("BUY PE")
    sell_ce_strike, sell_ce_lots = _ask_leg("SELL CE")
    sell_pe_strike, sell_pe_lots = _ask_leg("SELL PE")

    if not any([buy_ce_strike, buy_pe_strike, sell_ce_strike, sell_pe_strike]):
        raise ValueError("No legs entered -- you must enter a strike for at least one leg.")

    lot_size = LOT_SIZES[index]
    print(f"\n  -> Using {index} lot size = {lot_size} (hardcoded; confirm this is still current on the exchange)")

    max_loss = float(input("\nEnter max combined loss in rupees (exit trigger across active legs): "))
    if max_loss <= 0:
        raise ValueError("Max loss must be positive")

    return {
        "INDEX": index,
        "EXPIRY": expiry,
        "BUY_CE_STRIKE": buy_ce_strike,
        "BUY_PE_STRIKE": buy_pe_strike,
        "SELL_CE_STRIKE": sell_ce_strike,
        "SELL_PE_STRIKE": sell_pe_strike,
        "BUY_CE_LOTS": buy_ce_lots,
        "BUY_PE_LOTS": buy_pe_lots,
        "SELL_CE_LOTS": sell_ce_lots,
        "SELL_PE_LOTS": sell_pe_lots,
        "LOT_SIZE": lot_size,
        "MAX_LOSS": max_loss,
    }