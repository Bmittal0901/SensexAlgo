import math

def bullish_crossover(df):
    if len(df) < 51:
        return False

    prev = df.iloc[-3]
    curr = df.iloc[-2]

    ema20_prev = prev["ema20"]
    ema50_prev = prev["ema50"]
    ema20_curr = curr["ema20"]
    ema50_curr = curr["ema50"]

    if any(map(lambda x: x is None or (isinstance(x, float) and math.isnan(x)),
               [ema20_prev, ema50_prev, ema20_curr, ema50_curr])):
        return False

    return (ema20_prev <= ema50_prev) and (ema20_curr > ema50_curr)
