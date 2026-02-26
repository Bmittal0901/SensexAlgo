from kiteconnect import KiteConnect
from zerodha_config import API_KEY, API_SECRET

kite = KiteConnect(api_key=API_KEY)
print("Login URL:", kite.login_url())

# After you login and get request_token from the URL:
request_token = input("Paste request_token here: ").strip()

data = kite.generate_session(request_token, api_secret=API_SECRET)
access_token = data["access_token"]
print("ACCESS_TOKEN:", access_token)
