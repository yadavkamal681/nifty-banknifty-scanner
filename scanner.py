import os
import json
from datetime import datetime
from typing import List

import requests
import pytz
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
LIVE_SHEET = "LIVE_SIGNALS"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_gspread_client():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    info = json.loads(sa_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def open_sheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID not set")
    return get_gspread_client().open_by_key(SPREADSHEET_ID)


def ensure_live_sheet(sh):
    try:
        return sh.worksheet(LIVE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=LIVE_SHEET, rows="1000", cols="10")


def to_yahoo_ticker(symbol: str, is_index: bool) -> str:
    s = symbol.upper()
    if is_index:
        if s == "NIFTY":
            return "^NSEI"
        if s == "BANKNIFTY":
            return "^NSEBANK"
        if s == "SENSEX":
            return "^BSESN"
    return f"{s}.NS"


def fetch_last_close(symbol: str, is_index: bool) -> float:
    ticker = to_yahoo_ticker(symbol, is_index)
    df = yf.download(
        ticker,
        period="2d",
        interval="5m",
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        raise RuntimeError(f"No data for {symbol}")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    close_val = df.iloc[-1]["Close"]
    if isinstance(close_val, pd.Series):
        close_val = close_val.iloc[0]
    return float(close_val)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def main():
    sh = open_sheet()
    ws = ensure_live_sheet(sh)

    symbols = [
        ("NIFTY", True),
        ("BANKNIFTY", True),
    ]

    now = datetime.now(IST).isoformat()
    rows: List[List[str]] = [["TIMESTAMP", "SYMBOL", "IS_INDEX", "LAST_CLOSE"]]
    messages: List[str] = []

    for sym, is_index in symbols:
        try:
            last_close = fetch_last_close(sym, is_index)
            rows.append([now, sym, "INDEX" if is_index else "STOCK", f"{last_close:.2f}"])
            messages.append(f"{sym}: {last_close:.2f}")
        except Exception as e:
            rows.append([now, sym, "INDEX" if is_index else "STOCK", f"ERROR: {e}"])

    ws.clear()
    ws.update("A1", rows)

    if messages:
        message = chr(10).join(["Scanner update:"] + messages)
        send_telegram(message)


if __name__ == "__main__":
    main()
