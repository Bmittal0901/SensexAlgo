# strategy.py
#
# 4-leg structure:
#   BUY_CE, BUY_PE   -> long straddle at the same strike (buy_qty each)
#   SELL_CE, SELL_PE -> short legs at their own strikes (sell_qty each,
#                       sell_qty = 3x buy_qty)
#
# Loss convention (per user spec):
#   BUY leg  loss = entry_premium - current_close   (positive when price falls)
#   SELL leg loss = current_close - entry_premium    (positive when price rises)
#
# A negative "loss" for a leg means that leg is actually in profit.
# Combined loss = sum of each leg's (loss_per_unit * qty) across all 4 legs.

LEG_DIRECTIONS = {
    "BUY_CE": "BUY",
    "BUY_PE": "BUY",
    "SELL_CE": "SELL",
    "SELL_PE": "SELL",
}


def leg_loss_per_unit(direction, entry_price, current_price):
    """Loss per unit (before qty) for a single leg, given its direction."""
    if direction == "BUY":
        return entry_price - current_price
    elif direction == "SELL":
        return current_price - entry_price
    else:
        raise ValueError(f"Unknown leg direction: {direction}")


def compute_combined_loss(entry_prices, current_prices, qtys, leg_directions=LEG_DIRECTIONS):
    """
    entry_prices, current_prices: dict[leg_name -> price]
    qtys: dict[leg_name -> quantity]  (buy legs and sell legs can differ)
    leg_directions: dict[leg_name -> "BUY" | "SELL"]

    Returns combined loss in rupees. Positive = net loss, negative = net profit.
    """
    combined_loss = 0
    for leg, direction in leg_directions.items():
        per_unit_loss = leg_loss_per_unit(direction, entry_prices[leg], current_prices[leg])
        combined_loss += per_unit_loss * qtys[leg]
    return combined_loss


def should_exit(combined_loss, max_loss):
    """Exit as soon as combined_loss reaches or exceeds the max_loss threshold.

    Args:
        combined_loss: Current combined loss value
        max_loss: Loss threshold to trigger exit

    Returns:
        True if combined_loss >= max_loss
    """
    return combined_loss >= max_loss