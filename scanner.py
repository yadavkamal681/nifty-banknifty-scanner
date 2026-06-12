import os
import json
from datetime import datetime, timedelta
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
MARKET_INTELLIGENCE_SHEET = "MARKET_INTELLIGENCE"
TELEGRAM_STATE_SHEET = "TELEGRAM_STATE"
RISK_SETTINGS_SHEET = "RISK_SETTINGS"
EARNINGS_SHEET = "EARNINGS_CALENDAR"
SECTOR_SHEET = "SECTOR_STRENGTH"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
EMA_STRATEGY = "EMA_BREAKOUT"
DMA_STRATEGY = "DMA"
BOTH_STRATEGY = "BOTH"
SUMMARY_TIMES = ["09:10","09:25","10:00","11:00","11:30","12:00","12:30","13:00","13:30","14:00","14:30","15:20","20:30"]
FAST_ALERT_THRESHOLD = 32
HIGH_ALERT_THRESHOLD = 50
REMINDER_GAP_MINUTES = 12
DEFAULT_DAILY_ALERT_CAP = 12
SECTOR_MAP = {
    "BANKING": ["HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS","KOTAKBANK.NS"],
    "IT": ["TCS.NS","INFY.NS","WIPRO.NS","HCLTECH.NS","TECHM.NS"],
    "PHARMA": ["SUNPHARMA.NS","DRREDDY.NS","CIPLA.NS","DIVISLAB.NS","LUPIN.NS"],
    "AUTO": ["MARUTI.NS","TATAMOTORS.NS","M&M.NS","EICHERMOT.NS","BAJAJ-AUTO.NS"],
    "FMCG": ["HINDUNILVR.NS","ITC.NS","NESTLEIND.NS","BRITANNIA.NS","DABUR.NS"],
    "METAL": ["TATASTEEL.NS","JSWSTEEL.NS","HINDALCO.NS","VEDL.NS","NMDC.NS"],
    "ENERGY": ["RELIANCE.NS","ONGC.NS","BPCL.NS","IOC.NS","POWERGRID.NS"],
}
STOCK_TO_SECTOR = {
    "HDFCBANK":"BANKING","ICICIBANK":"BANKING","SBIN":"BANKING","AXISBANK":"BANKING","KOTAKBANK":"BANKING",
    "TCS":"IT","INFY":"IT","WIPRO":"IT","HCLTECH":"IT","TECHM":"IT",
    "SUNPHARMA":"PHARMA","DRREDDY":"PHARMA","CIPLA":"PHARMA","DIVISLAB":"PHARMA","LUPIN":"PHARMA",
    "MARUTI":"AUTO","TATAMOTORS":"AUTO","M&M":"AUTO","EICHERMOT":"AUTO","BAJAJ-AUTO":"AUTO",
    "HINDUNILVR":"FMCG","ITC":"FMCG","NESTLEIND":"FMCG","BRITANNIA":"FMCG","DABUR":"FMCG",
    "TATASTEEL":"METAL","JSWSTEEL":"METAL","HINDALCO":"METAL","VEDL":"METAL","NMDC":"METAL",
    "RELIANCE":"ENERGY","ONGC":"ENERGY","BPCL":"ENERGY","IOC":"ENERGY","POWERGRID":"ENERGY",
}
PRIORITY_SCORE_MAP = {"HIGH": 8, "MEDIUM": 3, "LOW": 0}

def get_gspread_client():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    info = json.loads(sa_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def open_sheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID not set")
    return get_gspread_client().open_by_key(SPREADSHEET_ID)

def ensure_sheet(sh, title: str, rows: str = "5000", cols: str = "200"):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

def write_table(ws, headers: List[str], rows: List[Dict]):
    values = [headers] + [[r.get(h, "") for h in headers] for r in rows]
    ws.clear()
    ws.update("A1", values)

def safe_float(v, default=0.0):
    try:
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default

def safe_int(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default

def to_yahoo_ticker(symbol: str, type_: str) -> str:
    s = symbol.upper().strip()
    t = type_.upper().strip()
    if t == "INDEX":
        if s == "NIFTY": return "^NSEI"
        if s == "BANKNIFTY": return "^NSEBANK"
        if s == "SENSEX": return "^BSESN"
    return f"{s}.NS"

def parse_strategy(value: str) -> str:
    strategy = str(value or "").strip().upper().replace(" ", "_")
    if strategy in ("", "EMA", "EMA_BREAKOUT"): return EMA_STRATEGY
    if strategy in ("DMA", "50_100_200_DMA", "50/100/200_DMA", "50/100/200DMA"): return DMA_STRATEGY
    if strategy in ("BOTH", "ALL"): return BOTH_STRATEGY
    return EMA_STRATEGY

def normalize_priority(v: str) -> str:
    p = str(v or "MEDIUM").strip().upper()
    return p if p in ("HIGH", "MEDIUM", "LOW") else "MEDIUM"

def load_config(sh) -> List[Dict]:
    ws = sh.worksheet(CONFIG_SHEET)
    rows = ws.get_all_records()
    out = []
    for row in rows:
        if str(row.get("ACTIVE", "")).strip().upper() != "TRUE":
            continue
        symbol = str(row.get("SYMBOL", "")).strip().upper()
        type_ = str(row.get("TYPE", "")).strip().upper()
        mode = str(row.get("MODE", "INTRADAY")).strip().upper()
        notes = str(row.get("NOTES", "")).strip()
        strategy = parse_strategy(row.get("STRATEGY", "EMA_BREAKOUT"))
        underlying = str(row.get("UNDERLYING FOR OPTIONS", symbol)).strip().upper() or symbol
        max_risk_mode = str(row.get("MAX RISK MODE", "NORMAL")).strip().upper() or "NORMAL"
        strike_offset_steps = safe_int(row.get("STRIKE OFFSET STEPS", 0), 0)
        priority = normalize_priority(row.get("PRIORITY", "MEDIUM"))
        if not symbol or type_ not in ("INDEX", "STOCK"):
            continue
        modes = ["INTRADAY", "SWING"] if mode == "BOTH" else [mode]
        for m in modes:
            out.append({"SYMBOL": symbol, "TYPE": type_, "MODE": m, "NOTES": notes, "STRATEGY": strategy, "UNDERLYING_FOR_OPTIONS": underlying, "MAX_RISK_MODE": max_risk_mode, "STRIKE_OFFSET_STEPS": strike_offset_steps, "PRIORITY": priority})
    return out

def fetch_df(symbol: str, type_: str, period: str, interval: str) -> pd.DataFrame:
    ticker = to_yahoo_ticker(symbol, type_)
    df = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False)
    if df.empty:
        raise RuntimeError(f"No data for {symbol}")
    df.index = pd.to_datetime(df.index)
    return df

def fetch_intraday_df(symbol: str, type_: str) -> pd.DataFrame:
    df = fetch_df(symbol, type_, "5d", "5m")
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

def nearest_strike(price: float, step: int) -> int:
    return int(round(price / step) * step) if price else 0

def option_step(symbol: str, type_: str, price: float) -> int:
    if symbol == "NIFTY": return 50
    if symbol in ("BANKNIFTY", "SENSEX"): return 100
    if price < 200: return 5
    if price < 1000: return 10
    if price < 3000: return 20
    return 50

def derive_support_resistance(df: pd.DataFrame, mode: str) -> Dict:
    high_s, low_s, close_s = get_series(df, "High"), get_series(df, "Low"), get_series(df, "Close")
    look = 20 if mode == "SWING" else 15
    supports = sorted(low_s.tail(look).nsmallest(3).tolist())
    resistances = sorted(high_s.tail(look).nlargest(3).tolist())
    pivot = round((float(high_s.iloc[-1]) + float(low_s.iloc[-1]) + float(close_s.iloc[-1])) / 3, 2)
    return {"SUPPORT_1": round(supports[0], 2) if len(supports) > 0 else "", "SUPPORT_2": round(supports[1], 2) if len(supports) > 1 else "", "SUPPORT_3": round(supports[2], 2) if len(supports) > 2 else "", "RESISTANCE_1": round(resistances[0], 2) if len(resistances) > 0 else "", "RESISTANCE_2": round(resistances[1], 2) if len(resistances) > 1 else "", "RESISTANCE_3": round(resistances[2], 2) if len(resistances) > 2 else "", "PIVOT": pivot}

def derive_ema_plan(df: pd.DataFrame, mode: str) -> Dict:
    close_s, high_s, low_s = get_series(df, "Close"), get_series(df, "High"), get_series(df, "Low")
    lookback = 20 if mode == "SWING" else 15
    recent_high, recent_low = float(high_s.tail(lookback).max()), float(low_s.tail(lookback).min())
    prev_high = float(high_s.tail(lookback + 1).iloc[:-1].max()) if len(high_s) > lookback else recent_high
    prev_low = float(low_s.tail(lookback + 1).iloc[:-1].min()) if len(low_s) > lookback else recent_low
    last_price = float(close_s.iloc[-1])
    prev_price = float(close_s.iloc[-2]) if len(close_s) >= 2 else last_price
    change_pct = ((last_price - prev_price) / prev_price * 100.0) if prev_price else 0.0
    ema9, ema21, ema50 = close_s.ewm(span=9, adjust=False).mean(), close_s.ewm(span=21, adjust=False).mean(), close_s.ewm(span=50, adjust=False).mean()
    atr_period = 14 if len(close_s) >= 14 else max(2, len(close_s) - 1)
    tr = pd.concat([high_s - low_s, (high_s - close_s.shift(1)).abs(), (low_s - close_s.shift(1)).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(atr_period).mean().iloc[-1]) if atr_period >= 2 else max(last_price * 0.005, 1.0)
    if pd.isna(atr) or atr <= 0: atr = max(last_price * 0.005, 1.0)
    bias, action, signal = "NEUTRAL", "HOLD", "WATCH"
    if float(ema9.iloc[-1]) > float(ema21.iloc[-1]) > float(ema50.iloc[-1]) and last_price >= prev_high:
        bias, action, signal = "BULLISH", "BUY", "BUY"
        entry = round(max(last_price, prev_high), 2)
        sl = round(max(entry - 1.2 * atr, recent_low), 2)
        risk = max(entry - sl, atr * 0.6)
        t1, t2, t3 = round(entry + risk, 2), round(entry + 2 * risk, 2), round(entry + 3 * risk, 2)
    elif float(ema9.iloc[-1]) < float(ema21.iloc[-1]) < float(ema50.iloc[-1]) and last_price <= prev_low:
        bias, action, signal = "BEARISH", "SELL", "SELL"
        entry = round(min(last_price, prev_low), 2)
        sl = round(min(entry + 1.2 * atr, recent_high), 2)
        risk = max(sl - entry, atr * 0.6)
        t1, t2, t3 = round(entry - risk, 2), round(entry - 2 * risk, 2), round(entry - 3 * risk, 2)
    else:
        entry, sl = round(last_price, 2), round(last_price - atr, 2)
        t1, t2, t3 = round(last_price + atr, 2), round(last_price + 2 * atr, 2), round(last_price + 3 * atr, 2)
    rr = round((t1 - entry) / (entry - sl), 2) if action == "BUY" and entry > sl else round((entry - t1) / (sl - entry), 2) if action == "SELL" and sl > entry else 0.0
    return {"EMA_LAST_PRICE": round(last_price,2),"EMA_CHANGE_PCT": round(change_pct,2),"EMA9": round(float(ema9.iloc[-1]),2),"EMA21": round(float(ema21.iloc[-1]),2),"EMA50": round(float(ema50.iloc[-1]),2),"EMA_RECENT_HIGH": round(recent_high,2),"EMA_RECENT_LOW": round(recent_low,2),"EMA_ATR": round(atr,2),"EMA_BIAS": bias,"EMA_ACTION": action,"EMA_SIGNAL": signal,"EMA_ENTRY": entry,"EMA_SL": sl,"EMA_TARGET_1": t1,"EMA_TARGET_2": t2,"EMA_TARGET_3": t3,"EMA_RR": rr}

def derive_dma_plan(df: pd.DataFrame, type_: str) -> Dict:
    close_s = get_series(df, "Close")
    if type_ != "STOCK":
        return {"DMA50":"","DMA100":"","DMA200":"","DMA_SIGNAL":"NA","DMA_ACTION":"NA","DMA_ENTRY":"","DMA_SL":"","DMA_TARGET_1":"","DMA_TARGET_2":"","DMA_TARGET_3":"","DMA_RR":"","DMA_STATUS":"DMA strategy only for STOCK"}
    dma50, dma100, dma200 = close_s.rolling(50).mean(), close_s.rolling(100).mean(), close_s.rolling(200).mean()
    d50 = float(dma50.iloc[-1]) if not pd.isna(dma50.iloc[-1]) else None
    d100 = float(dma100.iloc[-1]) if not pd.isna(dma100.iloc[-1]) else None
    d200 = float(dma200.iloc[-1]) if not pd.isna(dma200.iloc[-1]) else None
    last_close = float(close_s.iloc[-1]); prev_close = float(close_s.iloc[-2]) if len(close_s) >= 2 else last_close
    if d50 is None or d100 is None or d200 is None:
        return {"DMA50":round(d50,2) if d50 else "","DMA100":round(d100,2) if d100 else "","DMA200":round(d200,2) if d200 else "","DMA_SIGNAL":"NA","DMA_ACTION":"NA","DMA_ENTRY":"","DMA_SL":"","DMA_TARGET_1":"","DMA_TARGET_2":"","DMA_TARGET_3":"","DMA_RR":"","DMA_STATUS":"Not enough daily candles for 200 DMA"}
    action, signal, status = "HOLD", "WATCH", "DMA neutral"
    if last_close > d50 and last_close > d100 and last_close > d200:
        action, signal, status = "BUY", "DMA_BUY", "Close above 50/100/200 DMA"
        entry, sl = round(last_close, 2), round(d50, 2)
        risk = max(entry - sl, max(entry * 0.005, 0.1)); t1, t2, t3 = round(entry + risk,2), round(entry + 2 * risk,2), round(entry + 3 * risk,2)
    elif prev_close >= d50 and last_close < d50:
        action, signal, status = "SELL", "DMA_SELL_50", "Close broke below 50 DMA"
        entry, sl = round(last_close, 2), round(d50, 2)
        risk = max(sl - entry, max(entry * 0.005, 0.1)); t1, t2, t3 = round(entry - risk,2), round(entry - 2 * risk,2), round(entry - 3 * risk,2)
    else:
        entry, sl, t1, t2, t3 = round(last_close,2), round(d50,2), round(last_close,2), round(last_close,2), round(last_close,2)
    rr = round((t1 - entry) / (entry - sl), 2) if action == "BUY" and entry > sl else round((entry - t1) / (sl - entry), 2) if action == "SELL" and sl > entry else 0.0
    return {"DMA50":round(d50,2),"DMA100":round(d100,2),"DMA200":round(d200,2),"DMA_SIGNAL":signal,"DMA_ACTION":action,"DMA_ENTRY":entry,"DMA_SL":sl,"DMA_TARGET_1":t1,"DMA_TARGET_2":t2,"DMA_TARGET_3":t3,"DMA_RR":rr,"DMA_STATUS":status}

def option_side_from_action(action: str) -> str:
    if action == "BUY": return "CE"
    if action == "SELL": return "PE"
    return "WAIT"

def risk_multiplier(max_risk_mode: str) -> float:
    return 0.75 if max_risk_mode == "LOW" else 1.25 if max_risk_mode == "HIGH" else 1.0

def build_options_plan(symbol: str, type_: str, underlying: str, last_price: float, action: str, entry: float, sl: float, offset_steps: int, max_risk_mode: str) -> Dict:
    step = option_step(symbol, type_, last_price)
    base_strike = nearest_strike(last_price, step)
    side = option_side_from_action(action)
    strike = base_strike + (offset_steps * step if side == "CE" else -offset_steps * step if side == "PE" else 0)
    return {"OPTION_UNDERLYING": underlying,"OPTION_SIDE": side,"OPTION_BASE_STRIKE": base_strike,"OPTION_SUGGESTED_STRIKE": strike,"OPTION_STRIKE_STEP": step,"OPTION_MAX_RISK_MODE": max_risk_mode,"OPTION_STRIKE_OFFSET_STEPS": offset_steps,"OPTION_RISK_POINTS": round(abs(entry - sl) * risk_multiplier(max_risk_mode),2)}

def ensure_risk_settings(sh):
    ws = ensure_sheet(sh, RISK_SETTINGS_SHEET)
    if ws.get_all_values():
        return
    headers = ["TOTAL_CAPITAL","RISK_PERCENT_PER_TRADE","MAX_OPEN_TRADES","OPTION_LOT_SIZE_DEFAULT","MAX_DAILY_LOSS_PERCENT","DAILY_ALERT_CAP"]
    defaults = ["100000","2","3","75","5",str(DEFAULT_DAILY_ALERT_CAP)]
    ws.update("A1", [headers, defaults])

def load_risk_settings(sh) -> Dict:
    ensure_risk_settings(sh)
    ws = sh.worksheet(RISK_SETTINGS_SHEET)
    rows = ws.get_all_records()
    row = rows[0] if rows else {}
    return {"TOTAL_CAPITAL": safe_float(row.get("TOTAL_CAPITAL", 100000), 100000),"RISK_PERCENT_PER_TRADE": safe_float(row.get("RISK_PERCENT_PER_TRADE", 2), 2),"MAX_OPEN_TRADES": safe_int(row.get("MAX_OPEN_TRADES", 3), 3),"OPTION_LOT_SIZE_DEFAULT": safe_int(row.get("OPTION_LOT_SIZE_DEFAULT", 75), 75),"MAX_DAILY_LOSS_PERCENT": safe_float(row.get("MAX_DAILY_LOSS_PERCENT", 5), 5),"DAILY_ALERT_CAP": safe_int(row.get("DAILY_ALERT_CAP", DEFAULT_DAILY_ALERT_CAP), DEFAULT_DAILY_ALERT_CAP)}

def compute_position_sizing(entry: float, sl: float, risk_cfg: Dict, option_side: str) -> Dict:
    risk_per_trade_rupees = round(risk_cfg["TOTAL_CAPITAL"] * (risk_cfg["RISK_PERCENT_PER_TRADE"] / 100.0), 2)
    risk_per_unit = round(abs(entry - sl), 2)
    qty = int(risk_per_trade_rupees / risk_per_unit) if risk_per_unit > 0 else 0
    lot = risk_cfg["OPTION_LOT_SIZE_DEFAULT"] if option_side in ("CE", "PE") else 1
    lots = int(qty / lot) if lot > 0 else 0
    return {"RISK_PER_TRADE_RUPEES": risk_per_trade_rupees,"RISK_PER_UNIT": risk_per_unit,"SUGGESTED_QTY": qty,"SUGGESTED_LOTS": lots,"MAX_OPEN_TRADES": risk_cfg["MAX_OPEN_TRADES"],"MAX_DAILY_LOSS_PERCENT": risk_cfg["MAX_DAILY_LOSS_PERCENT"]}

def load_state_map(sh) -> Dict[str, Dict]:
    ws = ensure_sheet(sh, STATE_SHEET)
    rows = ws.get_all_records()
    out = {}
    for row in rows:
        key = f"{str(row.get('SYMBOL', '')).strip().upper()}|{str(row.get('MODE', '')).strip().upper()}"
        if key != "|": out[key] = row
    return out

def write_state_map(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, STATE_SHEET)
    headers = ["SYMBOL","TYPE","MODE","CONFIG_STRATEGY","EMA_LAST_SIGNAL","EMA_LAST_ACTION","DMA_LAST_SIGNAL","DMA_LAST_ACTION","LAST_PRICE","INVALIDATION_STATUS","LAST_UPDATED"]
    write_table(ws, headers, rows)

def load_telegram_state(sh) -> Dict[str, Dict]:
    ws = ensure_sheet(sh, TELEGRAM_STATE_SHEET)
    rows = ws.get_all_records()
    out = {}
    for r in rows:
        k = str(r.get("KEY", "")).strip()
        if k:
            out[k] = r
    return out

def write_telegram_state(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, TELEGRAM_STATE_SHEET)
    headers = ["KEY","SUMMARY_SLOT","SUMMARY_DATE","LAST_SENT_AT","LAST_ALERT_TYPE","LAST_SYMBOL","LAST_MODE","ALERT_COUNT_DATE","ALERT_COUNT"]
    write_table(ws, headers, rows)

def fetch_news_headlines(query: str, page_size: int = 3) -> List[str]:
    if NEWSAPI_KEY:
        try:
            r = requests.get("https://newsapi.org/v2/everything", params={"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": page_size, "apiKey": NEWSAPI_KEY}, timeout=10)
            data = r.json()
            return [a.get("title", "") for a in data.get("articles", []) if a.get("title")][:page_size]
        except Exception:
            return []
    return []

def simple_sentiment_score(texts: List[str]) -> int:
    pos = ["surge","gain","beat","up","strong","rally","bullish","growth","record","positive"]
    neg = ["fall","drop","miss","down","weak","crash","bearish","loss","negative","risk"]
    score = 0
    merged = " ".join(texts).lower()
    for w in pos: score += merged.count(w)
    for w in neg: score -= merged.count(w)
    return score

def market_snapshot() -> Dict:
    trackers = {"NIFTY":("^NSEI","5d","5m"),"BANKNIFTY":("^NSEBANK","5d","5m"),"SENSEX":("^BSESN","5d","5m"),"SPX":("^GSPC","5d","1d"),"NASDAQ":("^IXIC","5d","1d"),"VIX":("^VIX","5d","1d"),"USDINR":("INR=X","5d","1d"),"CRUDE":("CL=F","5d","1d")}
    out = {}
    for name, (ticker, period, interval) in trackers.items():
        try:
            df = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False)
            c = get_series(df, "Close")
            last_p = float(c.iloc[-1]); prev_p = float(c.iloc[-2]) if len(c) >= 2 else last_p
            chg = round(((last_p - prev_p) / prev_p) * 100.0, 2) if prev_p else 0.0
            out[name] = {"LAST": round(last_p, 2), "CHANGE_PCT": chg}
        except Exception:
            out[name] = {"LAST": "", "CHANGE_PCT": ""}
    nifty_chg = safe_float(out.get("NIFTY", {}).get("CHANGE_PCT", 0), 0)
    bank_chg = safe_float(out.get("BANKNIFTY", {}).get("CHANGE_PCT", 0), 0)
    vix_chg = safe_float(out.get("VIX", {}).get("CHANGE_PCT", 0), 0)
    nifty_mood = "BULLISH" if nifty_chg > 0 else "BEARISH" if nifty_chg < 0 else "NEUTRAL"
    bank_mood = "BULLISH" if bank_chg > 0 else "BEARISH" if bank_chg < 0 else "NEUTRAL"
    retail = "RISK-ON" if nifty_mood == "BULLISH" and bank_mood == "BULLISH" else "RISK-OFF" if nifty_mood == "BEARISH" and bank_mood == "BEARISH" else "MIXED"
    if abs(nifty_chg) > 0.6 and abs(bank_chg) > 0.6 and nifty_chg * bank_chg > 0:
        regime = "TREND_DAY"
    elif abs(nifty_chg) < 0.2 and abs(bank_chg) < 0.2:
        regime = "CHOPPY"
    elif vix_chg > 2:
        regime = "HIGH_VOLATILITY"
    else:
        regime = "MIXED"
    return {"DATA": out, "NIFTY_MOOD": nifty_mood, "BANKNIFTY_MOOD": bank_mood, "RETAIL_SENTIMENT": retail, "MARKET_REGIME": regime}

def current_time_bucket(now: datetime) -> str:
    hhmm = now.strftime("%H:%M")
    if "09:15" <= hhmm <= "10:30":
        return "OPEN_WINDOW"
    if "10:31" <= hhmm <= "13:30":
        return "MIDDAY"
    if "13:31" <= hhmm <= "15:15":
        return "LATE_SESSION"
    return "OFF_MARKET"

def time_rule_adjustments(bucket: str, mode: str) -> Dict:
    fast = FAST_ALERT_THRESHOLD
    high = HIGH_ALERT_THRESHOLD
    bonus = 0
    if bucket == "OPEN_WINDOW":
        bonus = 5 if mode == "INTRADAY" else 2
        fast -= 2
    elif bucket == "MIDDAY":
        bonus = 0
    elif bucket == "LATE_SESSION":
        bonus = -4 if mode == "INTRADAY" else 0
        fast += 4 if mode == "INTRADAY" else 1
        high += 2 if mode == "INTRADAY" else 0
    else:
        bonus = -8 if mode == "INTRADAY" else -2
        fast += 8
        high += 5
    return {"TIME_BUCKET": bucket, "TIME_BONUS": bonus, "FAST_THRESHOLD": fast, "HIGH_THRESHOLD": high}

def build_market_intelligence(snapshot: Dict, bucket: str) -> Dict:
    india_news = fetch_news_headlines("India stock market NSE Nifty Sensex", 3)
    global_news = fetch_news_headlines("US market Nasdaq S&P 500 inflation rates Fed", 3)
    earnings_news = fetch_news_headlines("India company earnings results NSE", 3)
    sector_news = fetch_news_headlines("India sectors banking IT pharma auto market news", 3)
    all_news = india_news + global_news + earnings_news + sector_news
    sentiment_score = simple_sentiment_score(all_news)
    sentiment_label = "POSITIVE" if sentiment_score > 1 else "NEGATIVE" if sentiment_score < -1 else "NEUTRAL"
    price_action = f"NIFTY {snapshot['DATA'].get('NIFTY',{}).get('CHANGE_PCT','')}% | BANKNIFTY {snapshot['DATA'].get('BANKNIFTY',{}).get('CHANGE_PCT','')}%"
    global_mood = f"SPX {snapshot['DATA'].get('SPX',{}).get('CHANGE_PCT','')}% | NDQ {snapshot['DATA'].get('NASDAQ',{}).get('CHANGE_PCT','')}% | VIX {snapshot['DATA'].get('VIX',{}).get('CHANGE_PCT','')}%"
    return {"TIMESTAMP": datetime.now(IST).isoformat(),"NIFTY_MOOD": snapshot["NIFTY_MOOD"],"BANKNIFTY_MOOD": snapshot["BANKNIFTY_MOOD"],"RETAIL_SENTIMENT": snapshot["RETAIL_SENTIMENT"],"MARKET_REGIME": snapshot["MARKET_REGIME"],"TIME_BUCKET": bucket,"NEWS_SENTIMENT": sentiment_label,"NEWS_SENTIMENT_SCORE": sentiment_score,"PRICE_ACTION_MOVEMENT": price_action,"GLOBAL_MARKET_MOOD": global_mood,"INDIA_NEWS_1": india_news[0] if len(india_news)>0 else "","GLOBAL_NEWS_1": global_news[0] if len(global_news)>0 else "","EARNINGS_NEWS_1": earnings_news[0] if len(earnings_news)>0 else "","SECTOR_NEWS_1": sector_news[0] if len(sector_news)>0 else ""}

def write_market_intelligence(sh, row: Dict):
    ws = ensure_sheet(sh, MARKET_INTELLIGENCE_SHEET)
    write_table(ws, list(row.keys()), [row])

def ensure_trading_journal(sh):
    ws = ensure_sheet(sh, JOURNAL_SHEET)
    if ws.get_all_values(): return
    headers = ["DATE","SYMBOL","MODE","SETUP_NAME","BIAS","ENTRY_PLAN","SL_PLAN","T1","T2","T3","ACTUAL_ENTRY","ACTUAL_EXIT","QTY","RISK_PER_TRADE","PNL","RESULT","MISTAKE","LESSON","EMOTION","NOTES"]
    ws.update("A1", [headers])

def fetch_earnings_calendar(config_rows: List[Dict]) -> List[Dict]:
    today = datetime.now(IST).date()
    out, seen = [], set()
    for cfg in config_rows:
        if cfg["TYPE"] != "STOCK":
            continue
        symbol = cfg["SYMBOL"]
        if symbol in seen:
            continue
        seen.add(symbol)
        ticker = to_yahoo_ticker(symbol, "STOCK")
        event_date = ""
        try:
            tk = yf.Ticker(ticker)
            cal = getattr(tk, 'calendar', None)
            if isinstance(cal, pd.DataFrame) and not cal.empty:
                vals = cal.values.flatten().tolist()
                for v in vals:
                    if hasattr(v, 'date'):
                        event_date = str(v.date())
                        break
            elif isinstance(cal, dict):
                for v in cal.values():
                    if hasattr(v, 'date'):
                        event_date = str(v.date())
                        break
        except Exception:
            event_date = ""
        days_left = ""
        if event_date:
            try:
                d = datetime.strptime(event_date[:10], "%Y-%m-%d").date()
                days_left = (d - today).days
            except Exception:
                days_left = ""
        out.append({"SYMBOL": symbol, "EVENT_TYPE": "EARNINGS", "EVENT_DATE": event_date, "DAYS_LEFT": days_left, "SOURCE": "yfinance", "STATUS": "UPCOMING" if event_date else "UNKNOWN"})
    return out

def write_earnings_calendar(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, EARNINGS_SHEET)
    headers = ["SYMBOL","EVENT_TYPE","EVENT_DATE","DAYS_LEFT","SOURCE","STATUS"]
    write_table(ws, headers, rows)

def earnings_lookup_map(rows: List[Dict]) -> Dict[str, Dict]:
    return {r.get("SYMBOL", ""): r for r in rows}

def detect_invalidation(row: Dict, prev_state: Dict) -> str:
    action = row.get("EMA_ACTION") if row.get("EMA_ACTION") in ("BUY", "SELL") else row.get("DMA_ACTION")
    price = safe_float(row.get("EMA_LAST_PRICE", 0), 0)
    sl = safe_float(row.get("EMA_SL", 0), 0) or safe_float(row.get("DMA_SL", 0), 0)
    prev_status = str(prev_state.get("INVALIDATION_STATUS", "")).upper()
    if action == "BUY" and sl and price < sl: return "INVALIDATED"
    if action == "SELL" and sl and price > sl: return "INVALIDATED"
    if action in ("BUY", "SELL"): return "ACTIVE"
    return prev_status or "IDLE"

def compute_sector_strength() -> List[Dict]:
    rows = []
    for sector, tickers in SECTOR_MAP.items():
        changes = []
        for ticker in tickers:
            try:
                df = yf.download(ticker, period="5d", interval="1d", auto_adjust=True, progress=False)
                c = get_series(df, "Close")
                if len(c) >= 2:
                    changes.append(((float(c.iloc[-1]) - float(c.iloc[-2])) / float(c.iloc[-2])) * 100.0)
            except Exception:
                pass
        avg_change = round(sum(changes) / len(changes), 2) if changes else 0.0
        mood = "STRONG" if avg_change > 0.7 else "WEAK" if avg_change < -0.7 else "NEUTRAL"
        rows.append({"SECTOR": sector, "AVG_CHANGE_PCT": avg_change, "SECTOR_MOOD": mood})
    return sorted(rows, key=lambda x: x["AVG_CHANGE_PCT"], reverse=True)

def write_sector_strength(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, SECTOR_SHEET)
    headers = ["SECTOR","AVG_CHANGE_PCT","SECTOR_MOOD"]
    write_table(ws, headers, rows)

def sector_map_lookup(rows: List[Dict]) -> Dict[str, Dict]:
    return {r["SECTOR"]: r for r in rows}

def compute_smart_score(row: Dict, market_regime: str, sector_info: Dict, priority: str, time_adj: Dict) -> Dict:
    base = 0.0
    if row.get("EMA_ACTION") == "BUY": base += 25
    elif row.get("EMA_ACTION") == "SELL": base += 22
    if row.get("DMA_ACTION") == "BUY": base += 20
    elif row.get("DMA_ACTION") == "SELL": base += 16
    if row.get("EMA_BIAS") == "BULLISH": base += 10
    elif row.get("EMA_BIAS") == "BEARISH": base += 8
    base += min(max(safe_float(row.get("EMA_RR", 0), 0), 0), 5) * 8
    base += min(abs(safe_float(row.get("EMA_CHANGE_PCT", 0), 0)), 5) * 3
    if row.get("MODE") == "INTRADAY": base += 4
    if row.get("CONFIG_STRATEGY") == "BOTH": base += 6
    if row.get("EARNINGS_DAYS_LEFT") not in ("", None):
        days_left = safe_int(row.get("EARNINGS_DAYS_LEFT", 99), 99)
        if days_left <= 3: base -= 8
        elif days_left <= 7: base -= 4
    sector_bonus = 0
    sector_mood = sector_info.get("SECTOR_MOOD", "NA") if sector_info else "NA"
    action = row.get("EMA_ACTION") if row.get("EMA_ACTION") in ("BUY", "SELL") else row.get("DMA_ACTION")
    if action == "BUY" and sector_mood == "STRONG": sector_bonus = 8
    elif action == "SELL" and sector_mood == "WEAK": sector_bonus = 8
    elif sector_mood == "NEUTRAL": sector_bonus = 0
    else: sector_bonus = -4 if sector_mood != "NA" else 0
    regime_bonus = 0
    if market_regime == "TREND_DAY" and action in ("BUY", "SELL"): regime_bonus = 8
    elif market_regime == "CHOPPY" and row.get("MODE") == "INTRADAY": regime_bonus = -6
    elif market_regime == "HIGH_VOLATILITY": regime_bonus = -4
    priority_bonus = PRIORITY_SCORE_MAP.get(priority, 3)
    time_bonus = time_adj["TIME_BONUS"]
    smart_score = round(base + sector_bonus + regime_bonus + priority_bonus + time_bonus, 2)
    quality = "HIGH" if smart_score >= time_adj["HIGH_THRESHOLD"] else "MEDIUM" if smart_score >= time_adj["FAST_THRESHOLD"] else "LOW"
    fast_alert = "YES" if smart_score >= time_adj["FAST_THRESHOLD"] and action in ("BUY", "SELL") else "NO"
    high_alert = "YES" if smart_score >= time_adj["HIGH_THRESHOLD"] and action in ("BUY", "SELL") else "NO"
    return {"BASE_SCORE": round(base,2), "SECTOR_BONUS": sector_bonus, "REGIME_BONUS": regime_bonus, "PRIORITY_BONUS": priority_bonus, "TIME_BONUS": time_bonus, "SMART_SCORE": smart_score, "ALERT_QUALITY": quality, "FAST_ALERT": fast_alert, "HIGH_ALERT": high_alert, "SECTOR_MOOD": sector_mood, "TIME_BUCKET": time_adj["TIME_BUCKET"], "FAST_THRESHOLD": time_adj["FAST_THRESHOLD"], "HIGH_THRESHOLD": time_adj["HIGH_THRESHOLD"]}

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception:
        pass

def build_top_table(rows: List[Dict], limit: int = 5) -> str:
    ranked = sorted(rows, key=lambda x: x.get("SMART_SCORE", 0), reverse=True)[:limit]
    if not ranked: return "No ranked setups"
    lines = ["SYMBOL | P | ACT | SCORE | TIME | ENTRY | OPT"]
    for r in ranked:
        act = r.get("EMA_ACTION") if r.get("EMA_ACTION") in ("BUY", "SELL") else r.get("DMA_ACTION")
        lines.append(f"{r['SYMBOL']} | {r.get('PRIORITY')} | {act} | {r.get('SMART_SCORE')} | {r.get('TIME_BUCKET')} | {r.get('EMA_ENTRY','')} | {r.get('OPTION_SIDE','')}-{r.get('OPTION_SUGGESTED_STRIKE','')}")
    return "\n".join(lines)

def build_summary_message(slot: str, mi: Dict, rows: List[Dict], sector_rows: List[Dict], limit: int) -> str:
    earnings_watch = [r for r in rows if safe_int(r.get("EARNINGS_DAYS_LEFT", 99), 99) <= 7]
    earnings_line = ", ".join([f"{r['SYMBOL']}({r.get('EARNINGS_DAYS_LEFT')})" for r in earnings_watch[:5]]) if earnings_watch else "No near earnings"
    top_sectors = ", ".join([f"{r['SECTOR']} {r['AVG_CHANGE_PCT']}%" for r in sector_rows[:3]]) if sector_rows else "No sector data"
    news_lines = [x for x in [mi.get("INDIA_NEWS_1",""), mi.get("GLOBAL_NEWS_1",""), mi.get("EARNINGS_NEWS_1",""), mi.get("SECTOR_NEWS_1","")] if x]
    news_block = "\n".join([f"- {x}" for x in news_lines[:4]]) if news_lines else "- No fresh news headlines"
    return f"[{slot} IST] Market Summary\nRegime: {mi.get('MARKET_REGIME','')} | Time: {mi.get('TIME_BUCKET','')}\nNIFTY: {mi.get('NIFTY_MOOD','')} | BANKNIFTY: {mi.get('BANKNIFTY_MOOD','')}\nRetail: {mi.get('RETAIL_SENTIMENT','')} | News: {mi.get('NEWS_SENTIMENT','')} ({mi.get('NEWS_SENTIMENT_SCORE','')})\nPrice action: {mi.get('PRICE_ACTION_MOVEMENT','')}\nTop sectors: {top_sectors}\nEarnings watch: {earnings_line}\n\nTop setups\n{build_top_table(rows, limit)}\n\nKey news\n{news_block}"

def eligible_summary_slot(now: datetime) -> str:
    chosen = ""
    hhmm = now.strftime("%H:%M")
    for slot in SUMMARY_TIMES:
        if hhmm >= slot:
            chosen = slot
    return chosen

def should_send_summary(slot: str, tg_state: Dict[str, Dict], today: str) -> bool:
    if not slot: return False
    prev = tg_state.get(f"SUMMARY|{slot}", {})
    return str(prev.get("SUMMARY_DATE", "")) != today

def should_send_reminder(key: str, tg_state: Dict[str, Dict], now: datetime) -> bool:
    prev = tg_state.get(key, {})
    ts = str(prev.get("LAST_SENT_AT", "")).strip()
    if not ts:
        return True
    try:
        last_dt = datetime.fromisoformat(ts)
        if last_dt.tzinfo is None:
            last_dt = IST.localize(last_dt)
        return now >= last_dt + timedelta(minutes=REMINDER_GAP_MINUTES)
    except Exception:
        return True

def get_daily_alert_count(tg_state: Dict[str, Dict], today: str) -> int:
    meta = tg_state.get("DAILY_ALERT_COUNTER", {})
    if str(meta.get("ALERT_COUNT_DATE", "")) == today:
        return safe_int(meta.get("ALERT_COUNT", 0), 0)
    return 0

def set_daily_alert_count(rows: List[Dict], today: str, count: int):
    rows[:] = [r for r in rows if r.get("KEY") != "DAILY_ALERT_COUNTER"]
    rows.append({"KEY": "DAILY_ALERT_COUNTER", "SUMMARY_SLOT": "", "SUMMARY_DATE": today, "LAST_SENT_AT": "", "LAST_ALERT_TYPE": "COUNTER", "LAST_SYMBOL": "", "LAST_MODE": "", "ALERT_COUNT_DATE": today, "ALERT_COUNT": count})

def write_live_signals(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, LIVE_SHEET)
    headers = ["TIMESTAMP","SYMBOL","TYPE","MODE","PRIORITY","CONFIG_STRATEGY","NOTES","UNDERLYING_FOR_OPTIONS","MAX_RISK_MODE","STRIKE_OFFSET_STEPS","SECTOR","SECTOR_MOOD","MARKET_REGIME","TIME_BUCKET","FAST_THRESHOLD","HIGH_THRESHOLD","EMA_LAST_PRICE","EMA_CHANGE_PCT","EMA9","EMA21","EMA50","EMA_RECENT_HIGH","EMA_RECENT_LOW","EMA_ATR","EMA_BIAS","EMA_ACTION","EMA_SIGNAL","EMA_ENTRY","EMA_SL","EMA_TARGET_1","EMA_TARGET_2","EMA_TARGET_3","EMA_RR","DMA50","DMA100","DMA200","DMA_SIGNAL","DMA_ACTION","DMA_ENTRY","DMA_SL","DMA_TARGET_1","DMA_TARGET_2","DMA_TARGET_3","DMA_RR","DMA_STATUS","SUPPORT_1","SUPPORT_2","SUPPORT_3","RESISTANCE_1","RESISTANCE_2","RESISTANCE_3","PIVOT","OPTION_UNDERLYING","OPTION_SIDE","OPTION_BASE_STRIKE","OPTION_SUGGESTED_STRIKE","OPTION_STRIKE_STEP","OPTION_MAX_RISK_MODE","OPTION_STRIKE_OFFSET_STEPS","OPTION_RISK_POINTS","RISK_PER_TRADE_RUPEES","RISK_PER_UNIT","SUGGESTED_QTY","SUGGESTED_LOTS","MAX_OPEN_TRADES","MAX_DAILY_LOSS_PERCENT","INVALIDATION_STATUS","EARNINGS_EVENT_DATE","EARNINGS_DAYS_LEFT","BASE_SCORE","SECTOR_BONUS","REGIME_BONUS","PRIORITY_BONUS","TIME_BONUS","SMART_SCORE","ALERT_QUALITY","FAST_ALERT","HIGH_ALERT","STATUS"]
    write_table(ws, headers, rows)

def write_ranked_signals(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, RANKED_SHEET)
    ranked = sorted(rows, key=lambda x: x.get("SMART_SCORE", 0), reverse=True)
    headers = ["TIMESTAMP","SYMBOL","TYPE","MODE","PRIORITY","SECTOR","SECTOR_MOOD","MARKET_REGIME","TIME_BUCKET","SMART_SCORE","ALERT_QUALITY","FAST_ALERT","HIGH_ALERT","EMA_ACTION","DMA_ACTION","EMA_ENTRY","EMA_SL","EMA_TARGET_1","EMA_RR","SUGGESTED_QTY","SUGGESTED_LOTS","INVALIDATION_STATUS","EARNINGS_EVENT_DATE","EARNINGS_DAYS_LEFT","SUPPORT_1","RESISTANCE_1","OPTION_SIDE","OPTION_SUGGESTED_STRIKE","OPTION_RISK_POINTS","STATUS"]
    write_table(ws, headers, ranked)

def main():
    sh = open_sheet()
    ensure_risk_settings(sh)
    risk_cfg = load_risk_settings(sh)
    cfg_rows = load_config(sh)
    state_map = load_state_map(sh)
    tg_state = load_telegram_state(sh)
    now = datetime.now(IST)
    now_iso = now.isoformat(); today = now.strftime("%Y-%m-%d")
    bucket = current_time_bucket(now)
    snapshot = market_snapshot()
    market_intel = build_market_intelligence(snapshot, bucket)
    earnings_rows = fetch_earnings_calendar(cfg_rows)
    earnings_map = earnings_lookup_map(earnings_rows)
    sector_rows = compute_sector_strength()
    sector_lookup = sector_map_lookup(sector_rows)
    live_rows, state_rows, candidate_alerts, high_alerts, invalidation_alerts = [], [], [], [], []
    new_tg_rows = [v for v in tg_state.values() if str(v.get('KEY','')).startswith('SUMMARY|') or str(v.get('KEY','')).startswith('ALERT|') or str(v.get('KEY','')) == 'DAILY_ALERT_COUNTER']
    daily_count = get_daily_alert_count(tg_state, today)
    daily_cap = risk_cfg.get("DAILY_ALERT_CAP", DEFAULT_DAILY_ALERT_CAP)
    for cfg in cfg_rows:
        symbol, type_, mode, notes, strategy, priority = cfg["SYMBOL"], cfg["TYPE"], cfg["MODE"], cfg["NOTES"], cfg["STRATEGY"], cfg["PRIORITY"]
        key = f"{symbol}|{mode}"
        prev = state_map.get(key, {})
        try:
            daily_df = fetch_df(symbol, type_, "1y", "1d")
            trade_df = fetch_intraday_df(symbol, type_) if mode == "INTRADAY" else daily_df
            ema_plan = derive_ema_plan(trade_df, mode)
            dma_plan = derive_dma_plan(daily_df, type_)
            sr_plan = derive_support_resistance(trade_df, mode)
            run_ema = strategy in (EMA_STRATEGY, BOTH_STRATEGY)
            run_dma = strategy in (DMA_STRATEGY, BOTH_STRATEGY)
            if not run_ema:
                for k in list(ema_plan.keys()): ema_plan[k] = "NA" if k in ("EMA_ACTION","EMA_SIGNAL","EMA_BIAS") else ""
            if not run_dma:
                for k in list(dma_plan.keys()): dma_plan[k] = "NA" if k in ("DMA_ACTION","DMA_SIGNAL") else ("Disabled by CONFIG strategy" if k == "DMA_STATUS" else "")
            pref_action = ema_plan.get("EMA_ACTION") if ema_plan.get("EMA_ACTION") in ("BUY","SELL") else dma_plan.get("DMA_ACTION")
            pref_entry = safe_float(ema_plan.get("EMA_ENTRY", 0), 0) or safe_float(dma_plan.get("DMA_ENTRY", 0), 0)
            pref_sl = safe_float(ema_plan.get("EMA_SL", 0), 0) or safe_float(dma_plan.get("DMA_SL", 0), 0)
            options_plan = build_options_plan(symbol, type_, cfg["UNDERLYING_FOR_OPTIONS"], safe_float(ema_plan.get("EMA_LAST_PRICE", 0), 0), pref_action, pref_entry, pref_sl, cfg["STRIKE_OFFSET_STEPS"], cfg["MAX_RISK_MODE"])
            pos_plan = compute_position_sizing(pref_entry, pref_sl, risk_cfg, options_plan.get("OPTION_SIDE", "WAIT"))
            earnings_info = earnings_map.get(symbol, {})
            sector_name = STOCK_TO_SECTOR.get(symbol, "INDEX") if type_ == "STOCK" else symbol
            sector_info = sector_lookup.get(sector_name, {"SECTOR_MOOD": "NA"})
            time_adj = time_rule_adjustments(bucket, mode)
            row = {"TIMESTAMP": now_iso, "SYMBOL": symbol, "TYPE": type_, "MODE": mode, "PRIORITY": priority, "CONFIG_STRATEGY": strategy, "NOTES": notes, "UNDERLYING_FOR_OPTIONS": cfg["UNDERLYING_FOR_OPTIONS"], "MAX_RISK_MODE": cfg["MAX_RISK_MODE"], "STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"], "SECTOR": sector_name, "MARKET_REGIME": snapshot["MARKET_REGIME"], **ema_plan, **dma_plan, **sr_plan, **options_plan, **pos_plan, "EARNINGS_EVENT_DATE": earnings_info.get("EVENT_DATE", ""), "EARNINGS_DAYS_LEFT": earnings_info.get("DAYS_LEFT", "")}
            row["INVALIDATION_STATUS"] = detect_invalidation(row, prev)
            row.update(compute_smart_score(row, snapshot["MARKET_REGIME"], sector_info, priority, time_adj))
            prev_ema_signal = str(prev.get("EMA_LAST_SIGNAL", "")).strip().upper()
            prev_ema_action = str(prev.get("EMA_LAST_ACTION", "")).strip().upper()
            prev_dma_signal = str(prev.get("DMA_LAST_SIGNAL", "")).strip().upper()
            prev_dma_action = str(prev.get("DMA_LAST_ACTION", "")).strip().upper()
            status_parts = []
            signal_changed = False
            if run_ema and (prev_ema_signal != str(ema_plan.get("EMA_SIGNAL", "")).upper() or prev_ema_action != str(ema_plan.get("EMA_ACTION", "")).upper()):
                status_parts.append("EMA_NEW_SIGNAL")
                signal_changed = True
            if run_dma and (prev_dma_signal != str(dma_plan.get("DMA_SIGNAL", "")).upper() or prev_dma_action != str(dma_plan.get("DMA_ACTION", "")).upper()):
                status_parts.append("DMA_NEW_SIGNAL")
                signal_changed = True
            if row["INVALIDATION_STATUS"] == "INVALIDATED" and str(prev.get("INVALIDATION_STATUS", "")).upper() != "INVALIDATED":
                status_parts.append("TRADE_INVALIDATED")
            if row["FAST_ALERT"] == "NO":
                status_parts.append("FAST_FILTERED")
            if row["HIGH_ALERT"] == "NO":
                status_parts.append("HIGH_FILTERED")
            if not status_parts:
                status_parts.append("UNCHANGED")
            row["STATUS"] = " | ".join(status_parts)
            live_rows.append(row)
            alert_key = f"ALERT|{symbol}|{mode}"
            action = row.get("EMA_ACTION") if row.get("EMA_ACTION") in ("BUY", "SELL") else row.get("DMA_ACTION")
            active_setup = action in ("BUY", "SELL") and row["INVALIDATION_STATUS"] != "INVALIDATED"
            reminder_ok = should_send_reminder(alert_key, tg_state, now)
            if row["FAST_ALERT"] == "YES" and active_setup and (signal_changed or reminder_ok):
                label = "HIGH CONVICTION" if row["HIGH_ALERT"] == "YES" else "FAST ALERT"
                row_message = f"{label}\n{row['SYMBOL']} | {row['MODE']} | Priority {row['PRIORITY']} | Score {row['SMART_SCORE']}\nTime {row['TIME_BUCKET']} | Sector {row['SECTOR']} {row['SECTOR_MOOD']} | Regime {row['MARKET_REGIME']}\nEntry {row.get('EMA_ENTRY')} | SL {row.get('EMA_SL')} | T1 {row.get('EMA_TARGET_1')}\nOpt {row.get('OPTION_SIDE')}-{row.get('OPTION_SUGGESTED_STRIKE')} | Qty {row.get('SUGGESTED_QTY')}"
                candidate_alerts.append((row["SMART_SCORE"], PRIORITY_SCORE_MAP.get(priority, 3), row_message, label, alert_key, symbol, mode, row["HIGH_ALERT"]))
            if "TRADE_INVALIDATED" in row["STATUS"]:
                invalidation_alerts.append(f"INVALIDATED\n{row['SYMBOL']} {row['MODE']} | Price {row.get('EMA_LAST_PRICE')} crossed SL {row.get('EMA_SL')}")
            state_rows.append({"SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "EMA_LAST_SIGNAL": ema_plan.get("EMA_SIGNAL", ""), "EMA_LAST_ACTION": ema_plan.get("EMA_ACTION", ""), "DMA_LAST_SIGNAL": dma_plan.get("DMA_SIGNAL", ""), "DMA_LAST_ACTION": dma_plan.get("DMA_ACTION", ""), "LAST_PRICE": ema_plan.get("EMA_LAST_PRICE", ""), "INVALIDATION_STATUS": row["INVALIDATION_STATUS"], "LAST_UPDATED": now_iso})
        except Exception as e:
            live_rows.append({"TIMESTAMP": now_iso, "SYMBOL": symbol, "TYPE": type_, "MODE": mode, "PRIORITY": priority, "CONFIG_STRATEGY": strategy, "NOTES": notes, "UNDERLYING_FOR_OPTIONS": cfg["UNDERLYING_FOR_OPTIONS"], "MAX_RISK_MODE": cfg["MAX_RISK_MODE"], "STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"], "STATUS": f"ERROR: {e}", "SMART_SCORE": 0})
            state_rows.append({"SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "EMA_LAST_SIGNAL": "ERROR", "EMA_LAST_ACTION": "ERROR", "DMA_LAST_SIGNAL": "ERROR", "DMA_LAST_ACTION": "ERROR", "LAST_PRICE": "", "INVALIDATION_STATUS": "ERROR", "LAST_UPDATED": now_iso})
    candidate_alerts.sort(key=lambda x: (x[1], x[0]), reverse=True)
    remaining_capacity = max(daily_cap - daily_count, 0)
    selected = candidate_alerts[:remaining_capacity]
    for _, _, message, label, alert_key, symbol, mode, is_high in selected:
        send_telegram(message)
        daily_count += 1
        new_tg_rows = [r for r in new_tg_rows if r.get("KEY") != alert_key]
        new_tg_rows.append({"KEY": alert_key, "SUMMARY_SLOT": "", "SUMMARY_DATE": today, "LAST_SENT_AT": now_iso, "LAST_ALERT_TYPE": label, "LAST_SYMBOL": symbol, "LAST_MODE": mode, "ALERT_COUNT_DATE": today, "ALERT_COUNT": daily_count})
    if invalidation_alerts:
        send_telegram("\n\n".join(invalidation_alerts[:10]))
    write_live_signals(sh, live_rows)
    write_ranked_signals(sh, live_rows)
    write_state_map(sh, state_rows)
    ensure_trading_journal(sh)
    write_market_intelligence(sh, market_intel)
    write_earnings_calendar(sh, earnings_rows)
    write_sector_strength(sh, sector_rows)
    slot = eligible_summary_slot(now)
    if should_send_summary(slot, tg_state, today):
        limit = 10 if slot == "20:30" else 5
        send_telegram(build_summary_message(slot, live_rows=live_rows, mi=market_intel, sector_rows=sector_rows, limit=limit))
        new_tg_rows = [r for r in new_tg_rows if r.get("KEY") != f"SUMMARY|{slot}"]
        new_tg_rows.append({"KEY": f"SUMMARY|{slot}", "SUMMARY_SLOT": slot, "SUMMARY_DATE": today, "LAST_SENT_AT": now_iso, "LAST_ALERT_TYPE": "SUMMARY", "LAST_SYMBOL": "", "LAST_MODE": "", "ALERT_COUNT_DATE": today, "ALERT_COUNT": daily_count})
    set_daily_alert_count(new_tg_rows, today, daily_count)
    write_telegram_state(sh, new_tg_rows)

if __name__ == "__main__":
    main()
