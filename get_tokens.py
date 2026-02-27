import pandas as pd
from kiteconnect import KiteConnect
from zerodha_config import API_KEY

ACCESS_TOKEN = input("Paste ACCESS_TOKEN: ").strip()
kite = KiteConnect(api_key=API_KEY)
kite.set_access_token(ACCESS_TOKEN)

instruments = pd.DataFrame(kite.instruments("BFO"))

ce_symbol = input("Enter CE tradingsymbol (e.g., SENSEX27FEB78000CE): ").strip().upper()
pe_symbol = input("Enter PE tradingsymbol (e.g., SENSEX27FEB78000PE): ").strip().upper()

print("CE token:", instruments[instruments["tradingsymbol"] == ce_symbol]["instrument_token"].values[0])
print("PE token:", instruments[instruments["tradingsymbol"] == pe_symbol]["instrument_token"].values[0])

