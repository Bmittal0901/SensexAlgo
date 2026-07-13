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
    Computes combined loss only for the legs that actually exist.
    """

    combined_loss = 0

    for leg in entry_prices:

        direction = leg_directions[leg]

        per_unit_loss = leg_loss_per_unit(
            direction,
            entry_prices[leg],
            current_prices[leg]
        )

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


# ---------------- Optional per-leg SL / target ----------------
#
# These are opt-in. If per_leg_stop_loss / per_leg_target are left as None
# (the default in bot_engine.py), the strategy behaves exactly as before:
# only the combined-loss threshold or a manual stop triggers an exit.

def leg_hit_stop_loss(direction, entry_price, current_price, qty, per_leg_stop_loss):
    """True if this single leg's own loss (in rupees) has reached per_leg_stop_loss."""
    if per_leg_stop_loss is None:
        return False
    leg_loss = leg_loss_per_unit(direction, entry_price, current_price) * qty
    return leg_loss >= per_leg_stop_loss


def leg_hit_target(direction, entry_price, current_price, qty, per_leg_target):
    """True if this single leg's own profit (in rupees) has reached per_leg_target."""
    if per_leg_target is None:
        return False
    leg_profit = -leg_loss_per_unit(direction, entry_price, current_price) * qty
    return leg_profit >= per_leg_target