import os
import json
from datetime import datetime
from typing import Dict, List

import requests
import pytz
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf

# Timezone for India
IST = pytz.timezone("Asia/Kolkata")

# Google Sheet configuration
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
LIVE_SHEET = "LIVE_SIGNALS"

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_gspread_client():
    """Create an authorized gspread client using service account JSON from env."""
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
    """Open the target Google Sheet by ID."""
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID not set")
    gc = get_gspread_client()
    return gc.open_by_key(SPREADSHEET_ID)


def ensure_live_sheet(sh):
    """Get or create the LIVE_SIGNALS worksheet."""
    try:
        ws = sh.worksheet(LIVE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=LIVE_SHEET, rows="1000", cols="10")
    return ws


def to_yahoo_ticker(symbol: str, is_index: bool) -> str:
    """Map our symbols to Yahoo Finance tickers."""
    s = symbol.upper()
    if is_index:
        if s == "NIFTY":
            return "^NSEI"
        if s == "BANKNIFTY":
            return "^NSEBANK"
        if s == "SENSEX":
            return "^BSESN"
    # default: Indian stock
    return f"{s}.NS"


def fetch_last_close(symbol: str, is_index: bool) -> float:
    """Fetch last 5‑minute close price for the symbol from Yahoo Finance."""
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

    # Localize to IST
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)

    last_row = df.iloc[-1]
    close_val = last_row["Close"]
    if isinstance(close_val, pd.Series):
        close_val = close_val.iloc[0]
    return float(close_val)


def send_telegram(message: str):
    """Send a Telegram message if BOT_TOKEN + CHAT_ID are set."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        # Ignore send errors so they don't break the run
        pass


def main():
    # Open sheet and worksheet
    sh = open_sheet()
    ws = ensure_live_sheet(sh)

    # Symbols we track in this minimal test
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
            rows.append(
                [now, sym, "INDEX" if is_index else "STOCK", f"{last_close:.2f}"]
            )
            messages.append(f"{sym}: {last_close:.2f}")
        except Exception as e:
            rows.append(
                [now, sym, "INDEX" if is_index else "STOCK", f"ERROR: {e}"]
            )

    # Write to Google Sheet
    ws.clear()
    ws.update("A1", rows)

    # Send Telegram summary
    if messages:
        send_telegram("Scanner update:
" + "
".join(messages))


if __name__ == "__main__":
    main()
