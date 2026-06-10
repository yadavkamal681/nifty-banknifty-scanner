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


def ensure_sheet(sh, title: str, rows: str = "1000", cols: str = "40"):
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
        modes = ["INTRADAY", "SWING"] if mode == "BOTH" else [mode]
        for m in modes:
            out.append({"SYMBOL": symbol, "TYPE": type_, "MODE": m, "NOTES": notes})
    return out


def fetch_ohlc(symbol: str, type_: str, mode: str) -> pd.DataFrame:
    ticker = to_yahoo_ticker(symbol, type_)
    period = "1y" if mode == "SWING" else "6mo"
    interval = "1d"
    df = yf.download(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False,
    )
    if df.empty:
        raise RuntimeError(f"No data for {symbol}")
    df.index = pd.to_datetime(df.index)
    return df


def get_series(df: pd.DataFrame, name: str) -> pd.Series:
    s = df[name]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return s.astype(float)


def market_closed(now_ist: datetime) -> bool:
    hm = now_ist.hour * 60 + now_ist.minute
    return hm >= (15 * 60 + 30)


def derive_base_plan(df: pd.DataFrame, mode: str) -> Dict:
    close_s = get_series(df, "Close")
    high_s = get_series(df, "High")
    low_s = get_series(df, "Low")

    lookback = 20 if mode == "SWING" else 15
    recent_high = float(high_s.tail(lookback).max())
    recent_low = float(low_s.tail(lookback).min())
    prev_high = float(high_s.tail(lookback + 1).iloc[:-1].max()) if len(high_s) > lookback else recent_high
    prev_low = float(low_s.tail(lookback + 1).iloc[:-1].min()) if len(low_s) > lookback else recent_low

    last_price = float(close_s.iloc[-1])
    prev_price = float(close_s.iloc[-2]) if len(close_s) >= 2 else last_price
    change_pct = ((last_price - prev_price) / prev_price * 100.0) if prev_price else 0.0

    ema9 = close_s.ewm(span=9, adjust=False).mean()
    ema21 = close_s.ewm(span=21, adjust=False).mean()
    ema50 = close_s.ewm(span=50, adjust=False).mean()

    ema9_last = float(ema9.iloc[-1])
    ema21_last = float(ema21.iloc[-1])
    ema50_last = float(ema50.iloc[-1])

    atr_period = 14 if len(close_s) >= 14 else max(2, len(close_s) - 1)
    tr = pd.concat([
        high_s - low_s,
        (high_s - close_s.shift(1)).abs(),
        (low_s - close_s.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(atr_period).mean().iloc[-1]) if atr_period >= 2 else max(last_price * 0.005, 1.0)
    if pd.isna(atr) or atr <= 0:
        atr = max(last_price * 0.005, 1.0)

    bias = "NEUTRAL"
    action = "HOLD"
    signal = "WATCH"

    bullish_trend = ema9_last > ema21_last > ema50_last
    bearish_trend = ema9_last < ema21_last < ema50_last
    breakout_up = last_price >= prev_high
    breakout_down = last_price <= prev_low

    if bullish_trend and breakout_up:
        bias = "BULLISH"
        action = "BUY"
        signal = "BUY"
        entry = round(max(last_price, prev_high), 2)
        sl = round(max(entry - 1.2 * atr, recent_low), 2)
        risk = max(entry - sl, atr * 0.6)
        target_1 = round(entry + risk, 2)
        target_2 = round(entry + 2 * risk, 2)
    elif bearish_trend and breakout_down:
        bias = "BEARISH"
        action = "SELL"
        signal = "SELL"
        entry = round(min(last_price, prev_low), 2)
        sl = round(min(entry + 1.2 * atr, recent_high), 2)
        risk = max(sl - entry, atr * 0.6)
        target_1 = round(entry - risk, 2)
        target_2 = round(entry - 2 * risk, 2)
    elif ema9_last > ema21_last and last_price > ema21_last:
        bias = "BULLISH"
        action = "HOLD"
        signal = "HOLD_LONG"
        entry = round(last_price, 2)
        sl = round(last_price - atr, 2)
        target_1 = round(last_price + atr, 2)
        target_2 = round(last_price + 2 * atr, 2)
    elif ema9_last < ema21_last and last_price < ema21_last:
        bias = "BEARISH"
        action = "HOLD"
        signal = "HOLD_SHORT"
        entry = round(last_price, 2)
        sl = round(last_price + atr, 2)
        target_1 = round(last_price - atr, 2)
        target_2 = round(last_price - 2 * atr, 2)
    else:
        entry = round(last_price, 2)
        sl = round(last_price - atr, 2)
        target_1 = round(last_price + atr, 2)
        target_2 = round(last_price + 2 * atr, 2)

    rr = 0.0
    if action == "BUY" and entry > sl:
        rr = round((target_1 - entry) / (entry - sl), 2)
    elif action == "SELL" and sl > entry:
        rr = round((entry - target_1) / (sl - entry), 2)

    return {
        "LAST_PRICE": round(last_price, 2),
        "CHANGE_PCT": round(change_pct, 2),
        "EMA9": round(ema9_last, 2),
        "EMA21": round(ema21_last, 2),
        "EMA50": round(ema50_last, 2),
        "RECENT_HIGH": round(recent_high, 2),
        "RECENT_LOW": round(recent_low, 2),
        "ATR": round(atr, 2),
        "BIAS": bias,
        "ACTION": action,
        "SIGNAL": signal,
        "ENTRY": round(entry, 2),
        "SL": round(sl, 2),
        "TARGET_1": round(target_1, 2),
        "TARGET_2": round(target_2, 2),
        "RR": rr,
        "STRATEGY": "EMA_BREAKOUT",
    }


def derive_dma_plan(df: pd.DataFrame, type_: str) -> Dict:
    close_s = get_series(df, "Close")
    if type_ != "STOCK":
        return {
            "DMA50": "",
            "DMA100": "",
            "DMA200": "",
            "DMA_SIGNAL": "NA",
            "DMA_ACTION": "NA",
            "DMA_ENTRY": "",
            "DMA_SL": "",
            "DMA_TARGET_1": "",
            "DMA_TARGET_2": "",
            "DMA_RR": "",
            "DMA_STATUS": "DMA strategy only for STOCK",
        }

    dma50 = close_s.rolling(50).mean()
    dma100 = close_s.rolling(100).mean()
    dma200 = close_s.rolling(200).mean()

    last_close = float(close_s.iloc[-1])
    prev_close = float(close_s.iloc[-2]) if len(close_s) >= 2 else last_close
    d50 = float(dma50.iloc[-1]) if not pd.isna(dma50.iloc[-1]) else None
    d100 = float(dma100.iloc[-1]) if not pd.isna(dma100.iloc[-1]) else None
    d200 = float(dma200.iloc[-1]) if not pd.isna(dma200.iloc[-1]) else None

    if d50 is None or d100 is None or d200 is None:
        return {
            "DMA50": round(d50, 2) if d50 else "",
            "DMA100": round(d100, 2) if d100 else "",
            "DMA200": round(d200, 2) if d200 else "",
            "DMA_SIGNAL": "NA",
            "DMA_ACTION": "NA",
            "DMA_ENTRY": "",
            "DMA_SL": "",
            "DMA_TARGET_1": "",
            "DMA_TARGET_2": "",
            "DMA_RR": "",
            "DMA_STATUS": "Not enough daily candles for 200 DMA",
        }

    action = "HOLD"
    signal = "WATCH"
    status = "DMA neutral"

    if last_close > d50 and last_close > d100 and last_close > d200:
        action = "BUY"
        signal = "DMA_BUY"
        status = "Close above 50/100/200 DMA"
        entry = round(last_close, 2)
        sl = round(d50, 2)
        risk = max(entry - sl, max(entry * 0.005, 0.1))
        target_1 = round(entry + risk, 2)
        target_2 = round(entry + 2 * risk, 2)
    elif prev_close >= d50 and last_close < d50:
        action = "SELL"
        signal = "DMA_SELL_50"
        status = "Close broke below 50 DMA"
        entry = round(last_close, 2)
        sl = round(d50, 2)
        risk = max(sl - entry, max(entry * 0.005, 0.1))
        target_1 = round(entry - risk, 2)
        target_2 = round(entry - 2 * risk, 2)
    elif prev_close >= d100 and last_close < d100:
        action = "SELL"
        signal = "DMA_SELL_100"
        status = "Close broke below 100 DMA"
        entry = round(last_close, 2)
        sl = round(d50, 2)
        risk = max(sl - entry, max(entry * 0.005, 0.1))
        target_1 = round(entry - risk, 2)
        target_2 = round(entry - 2 * risk, 2)
    else:
        entry = round(last_close, 2)
        sl = round(d50, 2)
        target_1 = round(last_close, 2)
        target_2 = round(last_close, 2)

    rr = 0.0
    if action == "BUY" and entry > sl:
        rr = round((target_1 - entry) / (entry - sl), 2)
    elif action == "SELL" and sl > entry:
        rr = round((entry - target_1) / (sl - entry), 2)

    return {
        "DMA50": round(d50, 2),
        "DMA100": round(d100, 2),
        "DMA200": round(d200, 2),
        "DMA_SIGNAL": signal,
        "DMA_ACTION": action,
        "DMA_ENTRY": round(entry, 2),
        "DMA_SL": round(sl, 2),
        "DMA_TARGET_1": round(target_1, 2),
        "DMA_TARGET_2": round(target_2, 2),
        "DMA_RR": rr,
        "DMA_STATUS": status,
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
        "LAST_ACTION",
        "DMA_LAST_SIGNAL",
        "DMA_LAST_ACTION",
        "LAST_PRICE",
        "ENTRY",
        "SL",
        "TARGET_1",
        "TARGET_2",
        "DMA_ENTRY",
        "DMA_SL",
        "DMA_TARGET_1",
        "DMA_TARGET_2",
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
        "EMA50",
        "RECENT_HIGH",
        "RECENT_LOW",
        "ATR",
        "BIAS",
        "ACTION",
        "SIGNAL",
        "ENTRY",
        "SL",
        "TARGET_1",
        "TARGET_2",
        "RR",
        "DMA50",
        "DMA100",
        "DMA200",
        "DMA_SIGNAL",
        "DMA_ACTION",
        "DMA_ENTRY",
        "DMA_SL",
        "DMA_TARGET_1",
        "DMA_TARGET_2",
        "DMA_RR",
        "DMA_STATUS",
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


def build_ema_alert(symbol: str, mode: str, plan: Dict) -> str:
    return (
        f"{symbol} [{mode}] {plan['ACTION']}\n"
        f"EMA strategy | Bias: {plan['BIAS']} | Signal: {plan['SIGNAL']}\n"
        f"LTP: {plan['LAST_PRICE']} | Entry: {plan['ENTRY']}\n"
        f"SL: {plan['SL']} | T1: {plan['TARGET_1']} | T2: {plan['TARGET_2']}"
    )


def build_dma_alert(symbol: str, mode: str, dma: Dict) -> str:
    return (
        f"{symbol} [{mode}] {dma['DMA_ACTION']}\n"
        f"50/100/200 DMA strategy | Signal: {dma['DMA_SIGNAL']}\n"
        f"Entry: {dma['DMA_ENTRY']} | SL: {dma['DMA_SL']}\n"
        f"T1: {dma['DMA_TARGET_1']} | T2: {dma['DMA_TARGET_2']}\n"
        f"50DMA: {dma['DMA50']} | 100DMA: {dma['DMA100']} | 200DMA: {dma['DMA200']}"
    )


def build_daily_dma_summary(rows: List[Dict]) -> str:
    buy_rows = [r for r in rows if r.get("DMA_ACTION") == "BUY"]
    sell_rows = [r for r in rows if str(r.get("DMA_ACTION", "")).startswith("SELL") or r.get("DMA_ACTION") == "SELL"]
    lines = ["Daily DMA closing list"]
    lines.append("")
    lines.append(f"BUY count: {len(buy_rows)}")
    for r in buy_rows[:25]:
        lines.append(f"- {r['SYMBOL']} | Entry {r['DMA_ENTRY']} | SL {r['DMA_SL']} | T2 {r['DMA_TARGET_2']}")
    lines.append("")
    lines.append(f"SELL count: {len(sell_rows)}")
    for r in sell_rows[:25]:
        lines.append(f"- {r['SYMBOL']} | Entry {r['DMA_ENTRY']} | SL {r['DMA_SL']} | T2 {r['DMA_TARGET_2']}")
    return "\n".join(lines)


def main():
    sh = open_sheet()
    config_rows = load_config(sh)
    state_map = load_state_map(sh)

    now = datetime.now(IST)
    now_iso = now.isoformat()
    live_rows: List[Dict] = []
    new_state_rows: List[Dict] = []
    alert_lines: List[str] = []
    dma_summary_rows: List[Dict] = []

    for cfg in config_rows:
        symbol = cfg["SYMBOL"]
        type_ = cfg["TYPE"]
        mode = cfg["MODE"]
        notes = cfg["NOTES"]
        key = f"{symbol}|{mode}"
        prev_state = state_map.get(key, {})

        try:
            df = fetch_ohlc(symbol, type_, mode)
            plan = derive_base_plan(df, mode)
            dma = derive_dma_plan(df, type_)

            prev_signal = str(prev_state.get("LAST_SIGNAL", "")).strip().upper()
            prev_action = str(prev_state.get("LAST_ACTION", "")).strip().upper()
            prev_dma_signal = str(prev_state.get("DMA_LAST_SIGNAL", "")).strip().upper()
            prev_dma_action = str(prev_state.get("DMA_LAST_ACTION", "")).strip().upper()

            status_parts = []
            if prev_signal != plan["SIGNAL"] or prev_action != plan["ACTION"]:
                status_parts.append("EMA_NEW_SIGNAL")
                if plan["ACTION"] in ("BUY", "SELL"):
                    alert_lines.append(build_ema_alert(symbol, mode, plan))
            if prev_dma_signal != str(dma.get("DMA_SIGNAL", "")).upper() or prev_dma_action != str(dma.get("DMA_ACTION", "")).upper():
                status_parts.append("DMA_NEW_SIGNAL")
                if dma.get("DMA_ACTION") in ("BUY", "SELL"):
                    alert_lines.append(build_dma_alert(symbol, mode, dma))
            if not status_parts:
                status_parts.append("UNCHANGED")

            row = {
                "TIMESTAMP": now_iso,
                "SYMBOL": symbol,
                "TYPE": type_,
                "MODE": mode,
                "LAST_PRICE": plan["LAST_PRICE"],
                "CHANGE_PCT": plan["CHANGE_PCT"],
                "EMA9": plan["EMA9"],
                "EMA21": plan["EMA21"],
                "EMA50": plan["EMA50"],
                "RECENT_HIGH": plan["RECENT_HIGH"],
                "RECENT_LOW": plan["RECENT_LOW"],
                "ATR": plan["ATR"],
                "BIAS": plan["BIAS"],
                "ACTION": plan["ACTION"],
                "SIGNAL": plan["SIGNAL"],
                "ENTRY": plan["ENTRY"],
                "SL": plan["SL"],
                "TARGET_1": plan["TARGET_1"],
                "TARGET_2": plan["TARGET_2"],
                "RR": plan["RR"],
                "DMA50": dma.get("DMA50", ""),
                "DMA100": dma.get("DMA100", ""),
                "DMA200": dma.get("DMA200", ""),
                "DMA_SIGNAL": dma.get("DMA_SIGNAL", ""),
                "DMA_ACTION": dma.get("DMA_ACTION", ""),
                "DMA_ENTRY": dma.get("DMA_ENTRY", ""),
                "DMA_SL": dma.get("DMA_SL", ""),
                "DMA_TARGET_1": dma.get("DMA_TARGET_1", ""),
                "DMA_TARGET_2": dma.get("DMA_TARGET_2", ""),
                "DMA_RR": dma.get("DMA_RR", ""),
                "DMA_STATUS": dma.get("DMA_STATUS", ""),
                "NOTES": notes,
                "STATUS": " | ".join(status_parts),
            }
            live_rows.append(row)
            dma_summary_rows.append(row)

            new_state_rows.append(
                {
                    "SYMBOL": symbol,
                    "TYPE": type_,
                    "MODE": mode,
                    "LAST_SIGNAL": plan["SIGNAL"],
                    "LAST_ACTION": plan["ACTION"],
                    "DMA_LAST_SIGNAL": dma.get("DMA_SIGNAL", ""),
                    "DMA_LAST_ACTION": dma.get("DMA_ACTION", ""),
                    "LAST_PRICE": plan["LAST_PRICE"],
                    "ENTRY": plan["ENTRY"],
                    "SL": plan["SL"],
                    "TARGET_1": plan["TARGET_1"],
                    "TARGET_2": plan["TARGET_2"],
                    "DMA_ENTRY": dma.get("DMA_ENTRY", ""),
                    "DMA_SL": dma.get("DMA_SL", ""),
                    "DMA_TARGET_1": dma.get("DMA_TARGET_1", ""),
                    "DMA_TARGET_2": dma.get("DMA_TARGET_2", ""),
                    "LAST_UPDATED": now_iso,
                }
            )
        except Exception as e:
            live_rows.append(
                {
                    "TIMESTAMP": now_iso,
                    "SYMBOL": symbol,
                    "TYPE": type_,
                    "MODE": mode,
                    "LAST_PRICE": "",
                    "CHANGE_PCT": "",
                    "EMA9": "",
                    "EMA21": "",
                    "EMA50": "",
                    "RECENT_HIGH": "",
                    "RECENT_LOW": "",
                    "ATR": "",
                    "BIAS": "",
                    "ACTION": "NO TRADE",
                    "SIGNAL": "ERROR",
                    "ENTRY": "",
                    "SL": "",
                    "TARGET_1": "",
                    "TARGET_2": "",
                    "RR": "",
                    "DMA50": "",
                    "DMA100": "",
                    "DMA200": "",
                    "DMA_SIGNAL": "ERROR",
                    "DMA_ACTION": "NO TRADE",
                    "DMA_ENTRY": "",
                    "DMA_SL": "",
                    "DMA_TARGET_1": "",
                    "DMA_TARGET_2": "",
                    "DMA_RR": "",
                    "DMA_STATUS": str(e),
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
                    "LAST_ACTION": "NO TRADE",
                    "DMA_LAST_SIGNAL": "ERROR",
                    "DMA_LAST_ACTION": "NO TRADE",
                    "LAST_PRICE": "",
                    "ENTRY": "",
                    "SL": "",
                    "TARGET_1": "",
                    "TARGET_2": "",
                    "DMA_ENTRY": "",
                    "DMA_SL": "",
                    "DMA_TARGET_1": "",
                    "DMA_TARGET_2": "",
                    "LAST_UPDATED": now_iso,
                }
            )

    write_live_signals(sh, live_rows)
    write_state_map(sh, new_state_rows)

    if alert_lines:
        message = "Scanner V3 strategy alerts\n\n" + "\n\n".join(alert_lines[:20])
        send_telegram(message)

    if market_closed(now):
        summary_message = build_daily_dma_summary(dma_summary_rows)
        send_telegram(summary_message)


if __name__ == "__main__":
    main()
