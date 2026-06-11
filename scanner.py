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
RANKED_SHEET = "RANKED_SIGNALS"
JOURNAL_SHEET = "TRADING_JOURNAL"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

EMA_STRATEGY = "EMA_BREAKOUT"
DMA_STRATEGY = "DMA"
BOTH_STRATEGY = "BOTH"


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


def ensure_sheet(sh, title: str, rows: str = "2000", cols: str = "80"):
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


def parse_strategy(value: str) -> str:
    strategy = str(value or "").strip().upper().replace(" ", "_")
    if strategy in ("", "EMA", "EMA_BREAKOUT"):
        return EMA_STRATEGY
    if strategy in ("DMA", "50_100_200_DMA", "50/100/200_DMA", "50/100/200DMA"):
        return DMA_STRATEGY
    if strategy in ("BOTH", "ALL"):
        return BOTH_STRATEGY
    return EMA_STRATEGY


def safe_float(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default


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
        strategy = parse_strategy(row.get("STRATEGY", "EMA_BREAKOUT"))
        underlying = str(row.get("UNDERLYING FOR OPTIONS", symbol)).strip().upper() or symbol
        max_risk_mode = str(row.get("MAX RISK MODE", "NORMAL")).strip().upper() or "NORMAL"
        strike_offset_steps = int(safe_float(row.get("STRIKE OFFSET STEPS", 0), 0))
        if not symbol or type_ not in ("INDEX", "STOCK"):
            continue
        if mode not in ("INTRADAY", "SWING", "BOTH"):
            mode = "INTRADAY"
        modes = ["INTRADAY", "SWING"] if mode == "BOTH" else [mode]
        for m in modes:
            out.append({
                "SYMBOL": symbol,
                "TYPE": type_,
                "MODE": m,
                "NOTES": notes,
                "STRATEGY": strategy,
                "UNDERLYING_FOR_OPTIONS": underlying,
                "MAX_RISK_MODE": max_risk_mode,
                "STRIKE_OFFSET_STEPS": strike_offset_steps,
            })
    return out


def fetch_daily_df(symbol: str, type_: str, period: str = "1y") -> pd.DataFrame:
    ticker = to_yahoo_ticker(symbol, type_)
    df = yf.download(ticker, period=period, interval="1d", auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"No daily data for {symbol}")
    df.index = pd.to_datetime(df.index)
    return df


def fetch_intraday_df(symbol: str, type_: str) -> pd.DataFrame:
    ticker = to_yahoo_ticker(symbol, type_)
    df = yf.download(ticker, period="5d", interval="5m", auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"No intraday data for {symbol}")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    else:
        df.index = df.index.tz_convert(IST)
    return df


def get_series(df: pd.DataFrame, name: str) -> pd.Series:
    s = df[name]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    return s.astype(float)


def option_step(symbol: str, type_: str, price: float) -> int:
    s = symbol.upper()
    if s == "NIFTY":
        return 50
    if s == "BANKNIFTY":
        return 100
    if s == "SENSEX":
        return 100
    if price < 200:
        return 5
    if price < 1000:
        return 10
    if price < 3000:
        return 20
    return 50


def nearest_strike(price: float, step: int) -> int:
    return int(round(price / step) * step)


def derive_support_resistance(df: pd.DataFrame, mode: str) -> Dict:
    high_s = get_series(df, "High")
    low_s = get_series(df, "Low")
    close_s = get_series(df, "Close")
    look = 20 if mode == "SWING" else 15
    supports = sorted(low_s.tail(look).nsmallest(3).tolist())
    resistances = sorted(high_s.tail(look).nlargest(3).tolist())
    pivot = round((float(high_s.iloc[-1]) + float(low_s.iloc[-1]) + float(close_s.iloc[-1])) / 3, 2)
    return {
        "SUPPORT_1": round(supports[0], 2) if len(supports) > 0 else "",
        "SUPPORT_2": round(supports[1], 2) if len(supports) > 1 else "",
        "SUPPORT_3": round(supports[2], 2) if len(supports) > 2 else "",
        "RESISTANCE_1": round(resistances[0], 2) if len(resistances) > 0 else "",
        "RESISTANCE_2": round(resistances[1], 2) if len(resistances) > 1 else "",
        "RESISTANCE_3": round(resistances[2], 2) if len(resistances) > 2 else "",
        "PIVOT": pivot,
    }


def derive_ema_plan(df: pd.DataFrame, mode: str) -> Dict:
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

    bias, action, signal = "NEUTRAL", "HOLD", "WATCH"
    if ema9_last > ema21_last > ema50_last and last_price >= prev_high:
        bias, action, signal = "BULLISH", "BUY", "BUY"
        entry = round(max(last_price, prev_high), 2)
        sl = round(max(entry - 1.2 * atr, recent_low), 2)
        risk = max(entry - sl, atr * 0.6)
        t1, t2, t3 = round(entry + risk, 2), round(entry + 2 * risk, 2), round(entry + 3 * risk, 2)
    elif ema9_last < ema21_last < ema50_last and last_price <= prev_low:
        bias, action, signal = "BEARISH", "SELL", "SELL"
        entry = round(min(last_price, prev_low), 2)
        sl = round(min(entry + 1.2 * atr, recent_high), 2)
        risk = max(sl - entry, atr * 0.6)
        t1, t2, t3 = round(entry - risk, 2), round(entry - 2 * risk, 2), round(entry - 3 * risk, 2)
    else:
        entry = round(last_price, 2)
        sl = round(last_price - atr, 2)
        t1, t2, t3 = round(last_price + atr, 2), round(last_price + 2 * atr, 2), round(last_price + 3 * atr, 2)
    rr = 0.0
    if action == "BUY" and entry > sl:
        rr = round((t1 - entry) / (entry - sl), 2)
    elif action == "SELL" and sl > entry:
        rr = round((entry - t1) / (sl - entry), 2)
    return {
        "EMA_LAST_PRICE": round(last_price, 2),
        "EMA_CHANGE_PCT": round(change_pct, 2),
        "EMA9": round(ema9_last, 2),
        "EMA21": round(ema21_last, 2),
        "EMA50": round(ema50_last, 2),
        "EMA_RECENT_HIGH": round(recent_high, 2),
        "EMA_RECENT_LOW": round(recent_low, 2),
        "EMA_ATR": round(atr, 2),
        "EMA_BIAS": bias,
        "EMA_ACTION": action,
        "EMA_SIGNAL": signal,
        "EMA_ENTRY": round(entry, 2),
        "EMA_SL": round(sl, 2),
        "EMA_TARGET_1": round(t1, 2),
        "EMA_TARGET_2": round(t2, 2),
        "EMA_TARGET_3": round(t3, 2),
        "EMA_RR": rr,
    }


def derive_dma_plan(df: pd.DataFrame, type_: str) -> Dict:
    close_s = get_series(df, "Close")
    if type_ != "STOCK":
        return {
            "DMA50": "", "DMA100": "", "DMA200": "", "DMA_SIGNAL": "NA", "DMA_ACTION": "NA",
            "DMA_ENTRY": "", "DMA_SL": "", "DMA_TARGET_1": "", "DMA_TARGET_2": "", "DMA_TARGET_3": "", "DMA_RR": "",
            "DMA_STATUS": "DMA strategy only for STOCK"
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
            "DMA50": round(d50, 2) if d50 else "", "DMA100": round(d100, 2) if d100 else "", "DMA200": round(d200, 2) if d200 else "",
            "DMA_SIGNAL": "NA", "DMA_ACTION": "NA", "DMA_ENTRY": "", "DMA_SL": "", "DMA_TARGET_1": "", "DMA_TARGET_2": "", "DMA_TARGET_3": "", "DMA_RR": "",
            "DMA_STATUS": "Not enough daily candles for 200 DMA"
        }
    action, signal, status = "HOLD", "WATCH", "DMA neutral"
    if last_close > d50 and last_close > d100 and last_close > d200:
        action, signal, status = "BUY", "DMA_BUY", "Close above 50/100/200 DMA"
        entry = round(last_close, 2)
        sl = round(d50, 2)
        risk = max(entry - sl, max(entry * 0.005, 0.1))
        t1, t2, t3 = round(entry + risk, 2), round(entry + 2 * risk, 2), round(entry + 3 * risk, 2)
    elif prev_close >= d50 and last_close < d50:
        action, signal, status = "SELL", "DMA_SELL_50", "Close broke below 50 DMA"
        entry = round(last_close, 2)
        sl = round(d50, 2)
        risk = max(sl - entry, max(entry * 0.005, 0.1))
        t1, t2, t3 = round(entry - risk, 2), round(entry - 2 * risk, 2), round(entry - 3 * risk, 2)
    elif prev_close >= d100 and last_close < d100:
        action, signal, status = "SELL", "DMA_SELL_100", "Close broke below 100 DMA"
        entry = round(last_close, 2)
        sl = round(d50, 2)
        risk = max(sl - entry, max(entry * 0.005, 0.1))
        t1, t2, t3 = round(entry - risk, 2), round(entry - 2 * risk, 2), round(entry - 3 * risk, 2)
    else:
        entry = round(last_close, 2)
        sl = round(d50, 2) if d50 is not None else ""
        t1, t2, t3 = round(last_close, 2), round(last_close, 2), round(last_close, 2)
    rr = 0.0
    if action == "BUY" and entry > sl:
        rr = round((t1 - entry) / (entry - sl), 2)
    elif action == "SELL" and sl > entry:
        rr = round((entry - t1) / (sl - entry), 2)
    return {
        "DMA50": round(d50, 2), "DMA100": round(d100, 2), "DMA200": round(d200, 2),
        "DMA_SIGNAL": signal, "DMA_ACTION": action, "DMA_ENTRY": round(entry, 2), "DMA_SL": sl,
        "DMA_TARGET_1": round(t1, 2), "DMA_TARGET_2": round(t2, 2), "DMA_TARGET_3": round(t3, 2), "DMA_RR": rr, "DMA_STATUS": status
    }


def option_side_from_action(action: str) -> str:
    if action == "BUY":
        return "CE"
    if action == "SELL":
        return "PE"
    return "WAIT"


def risk_multiplier(max_risk_mode: str) -> float:
    m = max_risk_mode.upper()
    if m == "LOW":
        return 0.75
    if m == "HIGH":
        return 1.25
    return 1.0


def build_options_plan(symbol: str, type_: str, underlying: str, last_price: float, action: str, entry: float, sl: float, offset_steps: int, max_risk_mode: str) -> Dict:
    step = option_step(symbol, type_, last_price)
    base_strike = nearest_strike(last_price, step)
    side = option_side_from_action(action)
    strike = base_strike + (offset_steps * step if side == "CE" else -offset_steps * step if side == "PE" else 0)
    risk_points = abs(entry - sl) * risk_multiplier(max_risk_mode)
    return {
        "OPTION_UNDERLYING": underlying,
        "OPTION_SIDE": side,
        "OPTION_BASE_STRIKE": base_strike,
        "OPTION_SUGGESTED_STRIKE": strike,
        "OPTION_STRIKE_STEP": step,
        "OPTION_MAX_RISK_MODE": max_risk_mode,
        "OPTION_STRIKE_OFFSET_STEPS": offset_steps,
        "OPTION_RISK_POINTS": round(risk_points, 2),
    }


def compute_signal_score(row: Dict) -> float:
    score = 0.0
    if row.get("EMA_ACTION") == "BUY":
        score += 25
    elif row.get("EMA_ACTION") == "SELL":
        score += 22
    if row.get("DMA_ACTION") == "BUY":
        score += 20
    elif row.get("DMA_ACTION") == "SELL":
        score += 16
    if row.get("EMA_BIAS") == "BULLISH":
        score += 10
    elif row.get("EMA_BIAS") == "BEARISH":
        score += 8
    score += min(max(safe_float(row.get("EMA_RR", 0), 0), 0), 5) * 8
    score += min(abs(safe_float(row.get("EMA_CHANGE_PCT", 0), 0)), 5) * 3
    if row.get("MODE") == "INTRADAY":
        score += 4
    if row.get("CONFIG_STRATEGY") == "BOTH":
        score += 6
    return round(score, 2)


def load_state_map(sh) -> Dict[str, Dict]:
    ws = ensure_sheet(sh, STATE_SHEET)
    rows = ws.get_all_records()
    out = {}
    for row in rows:
        key = f"{str(row.get('SYMBOL', '')).strip().upper()}|{str(row.get('MODE', '')).strip().upper()}"
        if key != "|":
            out[key] = row
    return out


def write_state_map(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, STATE_SHEET)
    headers = [
        "SYMBOL", "TYPE", "MODE", "CONFIG_STRATEGY",
        "EMA_LAST_SIGNAL", "EMA_LAST_ACTION", "DMA_LAST_SIGNAL", "DMA_LAST_ACTION",
        "LAST_UPDATED"
    ]
    values = [headers] + [[r.get(h, "") for h in headers] for r in rows]
    ws.clear()
    ws.update("A1", values)


def write_table(ws, headers: List[str], rows: List[Dict]):
    values = [headers] + [[r.get(h, "") for h in headers] for r in rows]
    ws.clear()
    ws.update("A1", values)


def write_live_signals(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, LIVE_SHEET)
    headers = [
        "TIMESTAMP", "SYMBOL", "TYPE", "MODE", "CONFIG_STRATEGY", "NOTES",
        "UNDERLYING_FOR_OPTIONS", "MAX_RISK_MODE", "STRIKE_OFFSET_STEPS",
        "EMA_LAST_PRICE", "EMA_CHANGE_PCT", "EMA9", "EMA21", "EMA50", "EMA_RECENT_HIGH", "EMA_RECENT_LOW", "EMA_ATR",
        "EMA_BIAS", "EMA_ACTION", "EMA_SIGNAL", "EMA_ENTRY", "EMA_SL", "EMA_TARGET_1", "EMA_TARGET_2", "EMA_TARGET_3", "EMA_RR",
        "DMA50", "DMA100", "DMA200", "DMA_SIGNAL", "DMA_ACTION", "DMA_ENTRY", "DMA_SL", "DMA_TARGET_1", "DMA_TARGET_2", "DMA_TARGET_3", "DMA_RR", "DMA_STATUS",
        "SUPPORT_1", "SUPPORT_2", "SUPPORT_3", "RESISTANCE_1", "RESISTANCE_2", "RESISTANCE_3", "PIVOT",
        "OPTION_UNDERLYING", "OPTION_SIDE", "OPTION_BASE_STRIKE", "OPTION_SUGGESTED_STRIKE", "OPTION_STRIKE_STEP", "OPTION_MAX_RISK_MODE", "OPTION_STRIKE_OFFSET_STEPS", "OPTION_RISK_POINTS",
        "SIGNAL_SCORE", "STATUS"
    ]
    write_table(ws, headers, rows)


def write_ranked_signals(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, RANKED_SHEET)
    ranked = sorted(rows, key=lambda x: x.get("SIGNAL_SCORE", 0), reverse=True)
    headers = [
        "TIMESTAMP", "SYMBOL", "TYPE", "MODE", "CONFIG_STRATEGY", "SIGNAL_SCORE", "EMA_BIAS", "EMA_ACTION", "DMA_ACTION",
        "EMA_ENTRY", "EMA_SL", "EMA_TARGET_1", "EMA_TARGET_2", "EMA_TARGET_3", "EMA_RR",
        "SUPPORT_1", "SUPPORT_2", "SUPPORT_3", "RESISTANCE_1", "RESISTANCE_2", "RESISTANCE_3", "PIVOT",
        "OPTION_SIDE", "OPTION_SUGGESTED_STRIKE", "OPTION_RISK_POINTS", "STATUS"
    ]
    write_table(ws, headers, ranked)


def ensure_trading_journal(sh):
    ws = ensure_sheet(sh, JOURNAL_SHEET)
    existing = ws.get_all_values()
    if existing:
        return
    headers = [
        "DATE", "SYMBOL", "MODE", "SETUP_NAME", "BIAS", "ENTRY_PLAN", "SL_PLAN", "T1", "T2", "T3",
        "ACTUAL_ENTRY", "ACTUAL_EXIT", "QTY", "RISK_PER_TRADE", "PNL", "RESULT", "MISTAKE", "LESSON", "EMOTION", "NOTES"
    ]
    ws.update("A1", [headers])


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception:
        pass


def build_alert(row: Dict) -> str:
    return (
        f"{row['SYMBOL']} [{row['MODE']}] | Score {row['SIGNAL_SCORE']}\n"
        f"EMA {row['EMA_ACTION']} | DMA {row['DMA_ACTION']}\n"
        f"Entry {row['EMA_ENTRY']} | SL {row['EMA_SL']} | T1 {row['EMA_TARGET_1']} | T2 {row['EMA_TARGET_2']} | T3 {row['EMA_TARGET_3']}\n"
        f"S1 {row['SUPPORT_1']} | R1 {row['RESISTANCE_1']}\n"
        f"Option {row['OPTION_SIDE']} {row['OPTION_SUGGESTED_STRIKE']}"
    )


def main():
    sh = open_sheet()
    config_rows = load_config(sh)
    state_map = load_state_map(sh)
    now = datetime.now(IST)
    now_iso = now.isoformat()
    live_rows, state_rows, alerts = [], [], []

    for cfg in config_rows:
        symbol, type_, mode, notes, strategy = cfg["SYMBOL"], cfg["TYPE"], cfg["MODE"], cfg["NOTES"], cfg["STRATEGY"]
        key = f"{symbol}|{mode}"
        prev = state_map.get(key, {})
        try:
            daily_df = fetch_daily_df(symbol, type_)
            trade_df = fetch_intraday_df(symbol, type_) if mode == "INTRADAY" else daily_df
            ema_plan = derive_ema_plan(trade_df, mode)
            dma_plan = derive_dma_plan(daily_df, type_)
            sr_plan = derive_support_resistance(trade_df, mode)

            run_ema = strategy in (EMA_STRATEGY, BOTH_STRATEGY)
            run_dma = strategy in (DMA_STRATEGY, BOTH_STRATEGY)
            if not run_ema:
                for k in list(ema_plan.keys()):
                    ema_plan[k] = "NA" if k in ("EMA_ACTION", "EMA_SIGNAL", "EMA_BIAS") else ""
            if not run_dma:
                for k in list(dma_plan.keys()):
                    dma_plan[k] = "NA" if k in ("DMA_ACTION", "DMA_SIGNAL") else ("Disabled by CONFIG strategy" if k == "DMA_STATUS" else "")

            preferred_action = ema_plan.get("EMA_ACTION") if run_ema and ema_plan.get("EMA_ACTION") in ("BUY", "SELL") else dma_plan.get("DMA_ACTION")
            preferred_entry = safe_float(ema_plan.get("EMA_ENTRY", 0), 0) or safe_float(dma_plan.get("DMA_ENTRY", 0), 0)
            preferred_sl = safe_float(ema_plan.get("EMA_SL", 0), 0) or safe_float(dma_plan.get("DMA_SL", 0), 0)
            options_plan = build_options_plan(
                symbol, type_, cfg["UNDERLYING_FOR_OPTIONS"], safe_float(ema_plan.get("EMA_LAST_PRICE", 0), 0),
                preferred_action, preferred_entry, preferred_sl, cfg["STRIKE_OFFSET_STEPS"], cfg["MAX_RISK_MODE"]
            )

            row = {
                "TIMESTAMP": now_iso,
                "SYMBOL": symbol,
                "TYPE": type_,
                "MODE": mode,
                "CONFIG_STRATEGY": strategy,
                "NOTES": notes,
                "UNDERLYING_FOR_OPTIONS": cfg["UNDERLYING_FOR_OPTIONS"],
                "MAX_RISK_MODE": cfg["MAX_RISK_MODE"],
                "STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"],
                **ema_plan,
                **dma_plan,
                **sr_plan,
                **options_plan,
            }
            row["SIGNAL_SCORE"] = compute_signal_score(row)

            prev_ema_signal = str(prev.get("EMA_LAST_SIGNAL", "")).strip().upper()
            prev_ema_action = str(prev.get("EMA_LAST_ACTION", "")).strip().upper()
            prev_dma_signal = str(prev.get("DMA_LAST_SIGNAL", "")).strip().upper()
            prev_dma_action = str(prev.get("DMA_LAST_ACTION", "")).strip().upper()
            status_parts = []
            if run_ema and (prev_ema_signal != str(ema_plan.get("EMA_SIGNAL", "")).upper() or prev_ema_action != str(ema_plan.get("EMA_ACTION", "")).upper()):
                status_parts.append("EMA_NEW_SIGNAL")
            if run_dma and (prev_dma_signal != str(dma_plan.get("DMA_SIGNAL", "")).upper() or prev_dma_action != str(dma_plan.get("DMA_ACTION", "")).upper()):
                status_parts.append("DMA_NEW_SIGNAL")
            if not status_parts:
                status_parts.append("UNCHANGED")
            row["STATUS"] = " | ".join(status_parts)

            live_rows.append(row)
            if row["SIGNAL_SCORE"] >= 35 and (row.get("EMA_ACTION") in ("BUY", "SELL") or row.get("DMA_ACTION") in ("BUY", "SELL")) and "UNCHANGED" not in row["STATUS"]:
                alerts.append(build_alert(row))

            state_rows.append({
                "SYMBOL": symbol,
                "TYPE": type_,
                "MODE": mode,
                "CONFIG_STRATEGY": strategy,
                "EMA_LAST_SIGNAL": ema_plan.get("EMA_SIGNAL", ""),
                "EMA_LAST_ACTION": ema_plan.get("EMA_ACTION", ""),
                "DMA_LAST_SIGNAL": dma_plan.get("DMA_SIGNAL", ""),
                "DMA_LAST_ACTION": dma_plan.get("DMA_ACTION", ""),
                "LAST_UPDATED": now_iso,
            })
        except Exception as e:
            row = {
                "TIMESTAMP": now_iso, "SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "NOTES": notes,
                "UNDERLYING_FOR_OPTIONS": cfg["UNDERLYING_FOR_OPTIONS"], "MAX_RISK_MODE": cfg["MAX_RISK_MODE"], "STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"],
                "EMA_LAST_PRICE": "", "EMA_CHANGE_PCT": "", "EMA9": "", "EMA21": "", "EMA50": "", "EMA_RECENT_HIGH": "", "EMA_RECENT_LOW": "", "EMA_ATR": "",
                "EMA_BIAS": "", "EMA_ACTION": "ERROR", "EMA_SIGNAL": "ERROR", "EMA_ENTRY": "", "EMA_SL": "", "EMA_TARGET_1": "", "EMA_TARGET_2": "", "EMA_TARGET_3": "", "EMA_RR": "",
                "DMA50": "", "DMA100": "", "DMA200": "", "DMA_SIGNAL": "ERROR", "DMA_ACTION": "ERROR", "DMA_ENTRY": "", "DMA_SL": "", "DMA_TARGET_1": "", "DMA_TARGET_2": "", "DMA_TARGET_3": "", "DMA_RR": "", "DMA_STATUS": str(e),
                "SUPPORT_1": "", "SUPPORT_2": "", "SUPPORT_3": "", "RESISTANCE_1": "", "RESISTANCE_2": "", "RESISTANCE_3": "", "PIVOT": "",
                "OPTION_UNDERLYING": cfg["UNDERLYING_FOR_OPTIONS"], "OPTION_SIDE": "WAIT", "OPTION_BASE_STRIKE": "", "OPTION_SUGGESTED_STRIKE": "", "OPTION_STRIKE_STEP": "", "OPTION_MAX_RISK_MODE": cfg["MAX_RISK_MODE"], "OPTION_STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"], "OPTION_RISK_POINTS": "",
                "SIGNAL_SCORE": 0, "STATUS": f"ERROR: {e}"
            }
            live_rows.append(row)
            state_rows.append({
                "SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy,
                "EMA_LAST_SIGNAL": "ERROR", "EMA_LAST_ACTION": "ERROR", "DMA_LAST_SIGNAL": "ERROR", "DMA_LAST_ACTION": "ERROR", "LAST_UPDATED": now_iso,
            })

    write_live_signals(sh, live_rows)
    write_ranked_signals(sh, live_rows)
    write_state_map(sh, state_rows)
    ensure_trading_journal(sh)

    if alerts:
        msg = "Scanner v4 alerts\n\n" + "\n\n".join(alerts[:10])
        send_telegram(msg)


if __name__ == "__main__":
    main()
