# inputs.py

SELL_LOT_MULTIPLIER = 3  # sell CE/PE lots = 3x the buy lots


def get_user_inputs():
    print("\n=== Multi-Leg Options Algo Setup ===\n")

    index = input("Select index (SENSEX / NIFTY): ").strip().upper()
    if index not in ["SENSEX", "NIFTY"]:
        raise ValueError("Index must be SENSEX or NIFTY")

    expiry = input("Enter expiry date (YYYY-MM-DD, must be a currently-listed expiry): ").strip()
    if not expiry:
        raise ValueError("Expiry is required")

    print(f"\nEnter strikes for {index}:")
    buy_ce_strike = int(input("  Strike for BUY CE: "))
    buy_pe_strike = int(input("  Strike for BUY PE: "))
    sell_ce_strike = int(input("  Strike for SELL CE: "))
    sell_pe_strike = int(input("  Strike for SELL PE: "))

    buy_lots = int(input("\nEnter number of lots for the BUY legs (CE + PE): "))
    sell_lots = buy_lots * SELL_LOT_MULTIPLIER
    print(f"  -> SELL CE and SELL PE lots set to {sell_lots} ({SELL_LOT_MULTIPLIER}x buy lots)")

    # Exchange lot sizes are revised periodically, so we ask rather than
    # hardcode a value that could go stale.
    lot_size = int(input(f"Enter {index} lot size (confirm current value on exchange): "))

    max_loss = float(input("Enter max combined loss in rupees (exit trigger for all 4 legs): "))

    if buy_lots <= 0:
        raise ValueError("Buy lots must be >= 1")

    if lot_size <= 0:
        raise ValueError("Lot size must be >= 1")

    if max_loss <= 0:
        raise ValueError("Max loss must be positive")

    return {
        "INDEX": index,
        "EXPIRY": expiry,
        "BUY_CE_STRIKE": buy_ce_strike,
        "BUY_PE_STRIKE": buy_pe_strike,
        "SELL_CE_STRIKE": sell_ce_strike,
        "SELL_PE_STRIKE": sell_pe_strike,
        "BUY_LOTS": buy_lots,
        "SELL_LOTS": sell_lots,
        "LOT_SIZE": lot_size,
        "MAX_LOSS": max_loss,
    }