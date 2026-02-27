# inputs.py

def get_user_inputs():
    print("\n Sensex Options EMA Algo Setup \n")

    call_strike = int(input("Enter CALL strike (e.g., 78000): "))
    put_strike = int(input("Enter PUT strike (e.g., 77000): "))
    lots = int(input("Enter number of lots: "))
    profit_points = int(input("Enter profit points (e.g., 50): "))
    stoploss_points = int(input("Enter stoploss points (e.g., 30): "))
    timeframe = input("Enter timeframe (3m / 5m / 15m): ").strip()

    LOT_SIZE = 20  # Sensex lot size

    if timeframe not in ["3m", "5m", "15m"]:
        raise ValueError("Timeframe must be '3m', '5m' or '15m'")

    if lots <= 0:
        raise ValueError("Lots must be >= 1")

    if profit_points <= 0 or stoploss_points <= 0:
        raise ValueError("Profit/Stoploss must be positive")

    return {
        "CALL_STRIKE": call_strike,
        "PUT_STRIKE": put_strike,
        "LOTS": lots,
        "LOT_SIZE": LOT_SIZE,
        "PROFIT_POINTS": profit_points,
        "STOPLOSS_POINTS": stoploss_points,
        "TIMEFRAME": timeframe
    }
