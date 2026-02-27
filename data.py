from datetime import datetime, timedelta
import pytz
IST = pytz.timezone("Asia/Kolkata")

def get_candles_zk(kite, instrument_token, timeframe="5minute", days=3):
    to_dt = datetime.now(IST).replace(tzinfo=None)
    from_dt = to_dt - timedelta(days=days)

    candles = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_dt,
        to_date=to_dt,
        interval=timeframe
    )
    return candles
