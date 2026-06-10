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

IST = pytz.timezone("Asia/Kolkata")

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
CONFIG_SHEET = "CONFIG"
LIVE_SHEET = "LIVE_SIGNALS"
STATE_SHEET = "STATE"

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


def ensure_sheet(sh, title: str, rows: str = "1000", cols: str = "20"):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def to_yahoo_ticker(symbol: str, type_: str) -> str:
    s = symbol.upper().strip()
    t = type_.upper().strip()
    if t == "INDEX":
        if s == "NIFTY":
            return "^NSEI"
        if s == "BANKNIFTY":
            return "^NSEBANK"
        if s == "SENSEX":
            return "^BSESN"
    return f"{s}.NS"


def load_config(sh) -> List[Dict]:
    ws = sh.worksheet(CONFIG_SHEET)
    rows = ws.get_all_records()
    out: List[Dict] = []
    for row in rows:
        active = str(row.get("ACTIVE", "")).strip().upper()
        if active != "TRUE":
            continue
        symbol = str(row.get("SYMBOL", "")).strip().upper()
        type_ = str(row.get("TYPE", "")).strip().upper()
        mode = str(row.get("MODE", "INTRADAY")).strip().upper()
        notes = str(row.get("NOTES", "")).strip()
        if not symbol or type_ not in ("INDEX", "STOCK"):
            continue
        if mode not in ("INTRADAY", "SWING", "BOTH"):
            mode = "INTRADAY"
        out.append(
            {
                "SYMBOL": symbol,
                "TYPE": type_,
                "MODE": mode,
                "NOTES": notes,
            }
        )
    return out


def fetch_symbol_snapshot(symbol: str, type_: str) -> Dict:
    ticker = to_yahoo_ticker(symbol, type_)
    df = yf.download(
        ticker,
        period="5d",
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

    close_series = df["Close"]
    high_series = df["High"]
    low_series = df["Low"]

    if isinstance(close_series, pd.DataFrame):
        close_series = close_series.iloc[:, 0]
    if isinstance(high_series, pd.DataFrame):
        high_series = high_series.iloc[:, 0]
    if isinstance(low_series, pd.DataFrame):
        low_series = low_series.iloc[:, 0]

    last_price = float(close_series.iloc[-1])
    prev_price = float(close_series.iloc[-2]) if len(close_series) >= 2 else last_price
    change_pct = ((last_price - prev_price) / prev_price * 100.0) if prev_price else 0.0

    ema9 = close_series.ewm(span=9, adjust=False).mean()
    ema21 = close_series.ewm(span=21, adjust=False).mean()
    ema9_last = float(ema9.iloc[-1])
    ema21_last = float(ema21.iloc[-1])

    direction = "SIDEWAYS"
    signal = "WATCH"
    if ema9_last > ema21_last and last_price > ema9_last:
        direction = "UP"
        signal = "BULLISH"
    elif ema9_last < ema21_last and last_price < ema9_last:
        direction = "DOWN"
        signal = "BEARISH"

    day_high = float(high_series.tail(75).max()) if len(high_series) >= 1 else last_price
    day_low = float(low_series.tail(75).min()) if len(low_series) >= 1 else last_price

    return {
        "LAST_PRICE": round(last_price, 2),
        "PREV_PRICE": round(prev_price, 2),
        "CHANGE_PCT": round(change_pct, 2),
        "EMA9": round(ema9_last, 2),
        "EMA21": round(ema21_last, 2),
        "DIRECTION": direction,
        "SIGNAL": signal,
        "DAY_HIGH": round(day_high, 2),
        "DAY_LOW": round(day_low, 2),
    }


def load_state_map(sh) -> Dict[str, Dict]:
    ws = ensure_sheet(sh, STATE_SHEET)
    rows = ws.get_all_records()
    state_map: Dict[str, Dict] = {}
    for row in rows:
        key = f"{str(row.get('SYMBOL', '')).strip().upper()}|{str(row.get('MODE', '')).strip().upper()}"
        if key == "|":
            continue
        state_map[key] = row
    return state_map


def write_state_map(sh, state_rows: List[Dict]):
    ws = ensure_sheet(sh, STATE_SHEET)
    headers = [
        "SYMBOL",
        "TYPE",
        "MODE",
        "LAST_SIGNAL",
        "LAST_PRICE",
        "LAST_UPDATED",
    ]
    values = [headers]
    for row in state_rows:
        values.append([row.get(h, "") for h in headers])
    ws.clear()
    ws.update("A1", values)


def write_live_signals(sh, live_rows: List[Dict]):
    ws = ensure_sheet(sh, LIVE_SHEET)
    headers = [
        "TIMESTAMP",
        "SYMBOL",
        "TYPE",
        "MODE",
        "LAST_PRICE",
        "CHANGE_PCT",
        "EMA9",
        "EMA21",
        "DIRECTION",
        "SIGNAL",
        "DAY_HIGH",
        "DAY_LOW",
        "NOTES",
        "STATUS",
    ]
    values = [headers]
    for row in live_rows:
        values.append([row.get(h, "") for h in headers])
    ws.clear()
    ws.update("A1", values)


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
    config_rows = load_config(sh)
    state_map = load_state_map(sh)

    now = datetime.now(IST).isoformat()
    live_rows: List[Dict] = []
    new_state_rows: List[Dict] = []
    alert_lines: List[str] = []

    for cfg in config_rows:
        symbol = cfg["SYMBOL"]
        type_ = cfg["TYPE"]
        mode = cfg["MODE"]
        notes = cfg["NOTES"]
        key = f"{symbol}|{mode}"
        prev_state = state_map.get(key, {})

        try:
            snap = fetch_symbol_snapshot(symbol, type_)
            prev_signal = str(prev_state.get("LAST_SIGNAL", "")).strip().upper()
            new_signal = snap["SIGNAL"]
            status = "UNCHANGED"
            if prev_signal != new_signal:
                status = "NEW_SIGNAL"
                alert_lines.append(
                    f"{symbol} ({mode}) -> {new_signal} | Price: {snap['LAST_PRICE']} | Dir: {snap['DIRECTION']}"
                )

            live_rows.append(
                {
                    "TIMESTAMP": now,
                    "SYMBOL": symbol,
                    "TYPE": type_,
                    "MODE": mode,
                    "LAST_PRICE": snap["LAST_PRICE"],
                    "CHANGE_PCT": snap["CHANGE_PCT"],
                    "EMA9": snap["EMA9"],
                    "EMA21": snap["EMA21"],
                    "DIRECTION": snap["DIRECTION"],
                    "SIGNAL": snap["SIGNAL"],
                    "DAY_HIGH": snap["DAY_HIGH"],
                    "DAY_LOW": snap["DAY_LOW"],
                    "NOTES": notes,
                    "STATUS": status,
                }
            )

            new_state_rows.append(
                {
                    "SYMBOL": symbol,
                    "TYPE": type_,
                    "MODE": mode,
                    "LAST_SIGNAL": snap["SIGNAL"],
                    "LAST_PRICE": snap["LAST_PRICE"],
                    "LAST_UPDATED": now,
                }
            )
        except Exception as e:
            live_rows.append(
                {
                    "TIMESTAMP": now,
                    "SYMBOL": symbol,
                    "TYPE": type_,
                    "MODE": mode,
                    "LAST_PRICE": "",
                    "CHANGE_PCT": "",
                    "EMA9": "",
                    "EMA21": "",
                    "DIRECTION": "",
                    "SIGNAL": "ERROR",
                    "DAY_HIGH": "",
                    "DAY_LOW": "",
                    "NOTES": notes,
                    "STATUS": f"ERROR: {e}",
                }
            )
            new_state_rows.append(
                {
                    "SYMBOL": symbol,
                    "TYPE": type_,
                    "MODE": mode,
                    "LAST_SIGNAL": "ERROR",
                    "LAST_PRICE": "",
                    "LAST_UPDATED": now,
                }
            )

    write_live_signals(sh, live_rows)
    write_state_map(sh, new_state_rows)

    if alert_lines:
        message = chr(10).join(["Scanner V2 updates:"] + alert_lines)
        send_telegram(message)


if __name__ == "__main__":
    main()
