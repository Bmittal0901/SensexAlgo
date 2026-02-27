# zerodha_client.py
from kiteconnect import KiteConnect
from dotenv import load_dotenv
import os

load_dotenv()
API_KEY = os.getenv("API_KEY")


def get_kite(access_token: str = None):
    kite = KiteConnect(api_key=API_KEY)

    # If no token passed, try reading from file
    if not access_token:
        if os.path.exists("access_token.txt"):
            with open("access_token.txt", "r") as f:
                access_token = f.read().strip()
            print("Access token loaded from file.")
        else:
            raise Exception("No access token found. Run auto_login.py first.")

    kite.set_access_token(access_token)
    return kite