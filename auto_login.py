# auto_login.py
import os
import pyotp
import requests
from kiteconnect import KiteConnect
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
USER_ID    = os.getenv("ZERODHA_USER_ID")
PASSWORD   = os.getenv("ZERODHA_PASSWORD")
TOTP_KEY   = os.getenv("ZERODHA_TOTP_KEY")


def get_access_token():
    session = requests.Session()
    kite = KiteConnect(api_key=API_KEY)

    # Step 1 — Submit credentials
    resp = session.post("https://kite.zerodha.com/api/login", data={
        "user_id":  USER_ID,
        "password": PASSWORD,
    })
    resp.raise_for_status()
    data = resp.json()

    if data.get("status") != "success":
        raise Exception(f"Login step 1 failed: {data}")

    request_id = data["data"]["request_id"]
    print(f"Login step 1 success. request_id: {request_id}")

    # Step 2 — Submit TOTP
    totp = pyotp.TOTP(TOTP_KEY).now()
    resp2 = session.post("https://kite.zerodha.com/api/twofa", data={
        "user_id":     USER_ID,
        "request_id":  request_id,
        "twofa_value": totp,
        "twofa_type":  "totp",
    })
    resp2.raise_for_status()
    data2 = resp2.json()

    if data2.get("status") != "success":
        raise Exception(f"TOTP failed: {data2}")

    print("TOTP verified successfully.")

    # Step 3 — Get request_token from redirect
    resp3 = session.get(
        f"https://kite.trade/connect/login?api_key={API_KEY}&v=3",
        allow_redirects=True
    )
    final_url = resp3.url

    if "request_token=" not in final_url:
        raise Exception(f"Could not get request_token. Final URL: {final_url}")

    request_token = final_url.split("request_token=")[1].split("&")[0]
    print(f"request_token obtained.")

    # Step 4 — Generate access token
    session_data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = session_data["access_token"]
    print("Access token generated successfully.")

    # Step 5 — Save to file
    with open("access_token.txt", "w") as f:
        f.write(access_token)
    print("Access token saved.")

    return access_token


if __name__ == "__main__":
    get_access_token()
    print("Login successful.")