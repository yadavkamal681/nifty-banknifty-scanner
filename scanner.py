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
MARKET_INTELLIGENCE_SHEET = "MARKET_INTELLIGENCE"
TELEGRAM_STATE_SHEET = "TELEGRAM_STATE"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

EMA_STRATEGY = "EMA_BREAKOUT"
DMA_STRATEGY = "DMA"
BOTH_STRATEGY = "BOTH"
SUMMARY_TIMES = ["09:10","09:25","10:00","11:00","11:30","12:00","12:30","13:00","13:30","14:00","14:30","15:20","20:30"]


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


def ensure_sheet(sh, title: str, rows: str = "3000", cols: str = "120"):
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
        strike_offset_steps = int(safe_float(row.get("STRIKE OFFSET STEPS", 0), 0))
        if not symbol or type_ not in ("INDEX", "STOCK"):
            continue
        modes = ["INTRADAY", "SWING"] if mode == "BOTH" else [mode]
        for m in modes:
            out.append({
                "SYMBOL": symbol, "TYPE": type_, "MODE": m, "NOTES": notes, "STRATEGY": strategy,
                "UNDERLYING_FOR_OPTIONS": underlying, "MAX_RISK_MODE": max_risk_mode, "STRIKE_OFFSET_STEPS": strike_offset_steps,
            })
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
    return int(round(price / step) * step)


def option_step(symbol: str, type_: str, price: float) -> int:
    if symbol == "NIFTY":
        return 50
    if symbol in ("BANKNIFTY", "SENSEX"):
        return 100
    if price < 200:
        return 5
    if price < 1000:
        return 10
    if price < 3000:
        return 20
    return 50


def derive_support_resistance(df: pd.DataFrame, mode: str) -> Dict:
    high_s = get_series(df, "High")
    low_s = get_series(df, "Low")
    close_s = get_series(df, "Close")
    look = 20 if mode == "SWING" else 15
    support_vals = sorted(low_s.tail(look).nsmallest(3).tolist())
    resistance_vals = sorted(high_s.tail(look).nlargest(3).tolist())
    pivot = round((float(high_s.iloc[-1]) + float(low_s.iloc[-1]) + float(close_s.iloc[-1])) / 3, 2)
    return {
        "SUPPORT_1": round(support_vals[0], 2) if len(support_vals) > 0 else "",
        "SUPPORT_2": round(support_vals[1], 2) if len(support_vals) > 1 else "",
        "SUPPORT_3": round(support_vals[2], 2) if len(support_vals) > 2 else "",
        "RESISTANCE_1": round(resistance_vals[0], 2) if len(resistance_vals) > 0 else "",
        "RESISTANCE_2": round(resistance_vals[1], 2) if len(resistance_vals) > 1 else "",
        "RESISTANCE_3": round(resistance_vals[2], 2) if len(resistance_vals) > 2 else "",
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
        entry = round(last_price, 2)
        sl = round(last_price - atr, 2)
        t1, t2, t3 = round(last_price + atr, 2), round(last_price + 2 * atr, 2), round(last_price + 3 * atr, 2)
    rr = 0.0
    if action == "BUY" and entry > sl:
        rr = round((t1 - entry) / (entry - sl), 2)
    elif action == "SELL" and sl > entry:
        rr = round((entry - t1) / (sl - entry), 2)
    return {
        "EMA_LAST_PRICE": round(last_price, 2), "EMA_CHANGE_PCT": round(change_pct, 2),
        "EMA9": round(float(ema9.iloc[-1]), 2), "EMA21": round(float(ema21.iloc[-1]), 2), "EMA50": round(float(ema50.iloc[-1]), 2),
        "EMA_RECENT_HIGH": round(recent_high, 2), "EMA_RECENT_LOW": round(recent_low, 2), "EMA_ATR": round(atr, 2),
        "EMA_BIAS": bias, "EMA_ACTION": action, "EMA_SIGNAL": signal,
        "EMA_ENTRY": entry, "EMA_SL": sl, "EMA_TARGET_1": t1, "EMA_TARGET_2": t2, "EMA_TARGET_3": t3, "EMA_RR": rr,
    }


def derive_dma_plan(df: pd.DataFrame, type_: str) -> Dict:
    close_s = get_series(df, "Close")
    if type_ != "STOCK":
        return {"DMA50":"","DMA100":"","DMA200":"","DMA_SIGNAL":"NA","DMA_ACTION":"NA","DMA_ENTRY":"","DMA_SL":"","DMA_TARGET_1":"","DMA_TARGET_2":"","DMA_TARGET_3":"","DMA_RR":"","DMA_STATUS":"DMA strategy only for STOCK"}
    dma50 = close_s.rolling(50).mean()
    dma100 = close_s.rolling(100).mean()
    dma200 = close_s.rolling(200).mean()
    d50 = float(dma50.iloc[-1]) if not pd.isna(dma50.iloc[-1]) else None
    d100 = float(dma100.iloc[-1]) if not pd.isna(dma100.iloc[-1]) else None
    d200 = float(dma200.iloc[-1]) if not pd.isna(dma200.iloc[-1]) else None
    last_close = float(close_s.iloc[-1])
    prev_close = float(close_s.iloc[-2]) if len(close_s) >= 2 else last_close
    if d50 is None or d100 is None or d200 is None:
        return {"DMA50":round(d50,2) if d50 else "","DMA100":round(d100,2) if d100 else "","DMA200":round(d200,2) if d200 else "","DMA_SIGNAL":"NA","DMA_ACTION":"NA","DMA_ENTRY":"","DMA_SL":"","DMA_TARGET_1":"","DMA_TARGET_2":"","DMA_TARGET_3":"","DMA_RR":"","DMA_STATUS":"Not enough daily candles for 200 DMA"}
    action, signal, status = "HOLD", "WATCH", "DMA neutral"
    if last_close > d50 and last_close > d100 and last_close > d200:
        action, signal, status = "BUY", "DMA_BUY", "Close above 50/100/200 DMA"
        entry, sl = round(last_close, 2), round(d50, 2)
        risk = max(entry - sl, max(entry * 0.005, 0.1))
        t1, t2, t3 = round(entry + risk, 2), round(entry + 2 * risk, 2), round(entry + 3 * risk, 2)
    elif prev_close >= d50 and last_close < d50:
        action, signal, status = "SELL", "DMA_SELL_50", "Close broke below 50 DMA"
        entry, sl = round(last_close, 2), round(d50, 2)
        risk = max(sl - entry, max(entry * 0.005, 0.1))
        t1, t2, t3 = round(entry - risk, 2), round(entry - 2 * risk, 2), round(entry - 3 * risk, 2)
    else:
        entry, sl, t1, t2, t3 = round(last_close, 2), round(d50, 2), round(last_close, 2), round(last_close, 2), round(last_close, 2)
    rr = round((t1 - entry) / (entry - sl), 2) if action == "BUY" and entry > sl else round((entry - t1) / (sl - entry), 2) if action == "SELL" and sl > entry else 0.0
    return {"DMA50":round(d50,2),"DMA100":round(d100,2),"DMA200":round(d200,2),"DMA_SIGNAL":signal,"DMA_ACTION":action,"DMA_ENTRY":entry,"DMA_SL":sl,"DMA_TARGET_1":t1,"DMA_TARGET_2":t2,"DMA_TARGET_3":t3,"DMA_RR":rr,"DMA_STATUS":status}


def compute_signal_score(row: Dict) -> float:
    score = 0.0
    if row.get("EMA_ACTION") == "BUY": score += 25
    elif row.get("EMA_ACTION") == "SELL": score += 22
    if row.get("DMA_ACTION") == "BUY": score += 20
    elif row.get("DMA_ACTION") == "SELL": score += 16
    if row.get("EMA_BIAS") == "BULLISH": score += 10
    elif row.get("EMA_BIAS") == "BEARISH": score += 8
    score += min(max(safe_float(row.get("EMA_RR", 0), 0), 0), 5) * 8
    score += min(abs(safe_float(row.get("EMA_CHANGE_PCT", 0), 0)), 5) * 3
    if row.get("MODE") == "INTRADAY": score += 4
    if row.get("CONFIG_STRATEGY") == "BOTH": score += 6
    return round(score, 2)


def option_side_from_action(action: str) -> str:
    if action == "BUY": return "CE"
    if action == "SELL": return "PE"
    return "WAIT"


def risk_multiplier(max_risk_mode: str) -> float:
    return 0.75 if max_risk_mode == "LOW" else 1.25 if max_risk_mode == "HIGH" else 1.0


def build_options_plan(symbol: str, type_: str, underlying: str, last_price: float, action: str, entry: float, sl: float, offset_steps: int, max_risk_mode: str) -> Dict:
    step = option_step(symbol, type_, last_price)
    base_strike = nearest_strike(last_price, step) if last_price else 0
    side = option_side_from_action(action)
    strike = base_strike + (offset_steps * step if side == "CE" else -offset_steps * step if side == "PE" else 0)
    return {
        "OPTION_UNDERLYING": underlying, "OPTION_SIDE": side, "OPTION_BASE_STRIKE": base_strike,
        "OPTION_SUGGESTED_STRIKE": strike, "OPTION_STRIKE_STEP": step, "OPTION_MAX_RISK_MODE": max_risk_mode,
        "OPTION_STRIKE_OFFSET_STEPS": offset_steps, "OPTION_RISK_POINTS": round(abs(entry - sl) * risk_multiplier(max_risk_mode), 2),
    }


def fetch_news_headlines(query: str, page_size: int = 3) -> List[str]:
    if NEWSAPI_KEY:
        try:
            url = "https://newsapi.org/v2/everything"
            params = {"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": page_size, "apiKey": NEWSAPI_KEY}
            r = requests.get(url, params=params, timeout=10)
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
    trackers = {
        "NIFTY": ("^NSEI", "5d", "5m"),
        "BANKNIFTY": ("^NSEBANK", "5d", "5m"),
        "SENSEX": ("^BSESN", "5d", "5m"),
        "SPX": ("^GSPC", "5d", "1d"),
        "NASDAQ": ("^IXIC", "5d", "1d"),
        "VIX": ("^VIX", "5d", "1d"),
        "USDINR": ("INR=X", "5d", "1d"),
        "CRUDE": ("CL=F", "5d", "1d"),
    }
    out = {}
    for name, (ticker, period, interval) in trackers.items():
        try:
            df = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False)
            c = get_series(df, "Close")
            last_p = float(c.iloc[-1])
            prev_p = float(c.iloc[-2]) if len(c) >= 2 else last_p
            chg = round(((last_p - prev_p) / prev_p) * 100.0, 2) if prev_p else 0.0
            out[name] = {"LAST": round(last_p, 2), "CHANGE_PCT": chg}
        except Exception:
            out[name] = {"LAST": "", "CHANGE_PCT": ""}
    nifty_mood = "BULLISH" if safe_float(out.get("NIFTY", {}).get("CHANGE_PCT", 0), 0) > 0 else "BEARISH" if safe_float(out.get("NIFTY", {}).get("CHANGE_PCT", 0), 0) < 0 else "NEUTRAL"
    banknifty_mood = "BULLISH" if safe_float(out.get("BANKNIFTY", {}).get("CHANGE_PCT", 0), 0) > 0 else "BEARISH" if safe_float(out.get("BANKNIFTY", {}).get("CHANGE_PCT", 0), 0) < 0 else "NEUTRAL"
    retail_sentiment = "RISK-ON" if nifty_mood == "BULLISH" and banknifty_mood == "BULLISH" else "RISK-OFF" if nifty_mood == "BEARISH" and banknifty_mood == "BEARISH" else "MIXED"
    return {"DATA": out, "NIFTY_MOOD": nifty_mood, "BANKNIFTY_MOOD": banknifty_mood, "RETAIL_SENTIMENT": retail_sentiment}


def build_market_intelligence(snapshot: Dict) -> Dict:
    india_news = fetch_news_headlines("India stock market NSE Nifty Sensex", 3)
    global_news = fetch_news_headlines("US market Nasdaq S&P 500 inflation rates Fed", 3)
    earnings_news = fetch_news_headlines("India company earnings results NSE", 3)
    sector_news = fetch_news_headlines("India sectors banking IT pharma auto market news", 3)
    all_news = india_news + global_news + earnings_news + sector_news
    sentiment_score = simple_sentiment_score(all_news)
    sentiment_label = "POSITIVE" if sentiment_score > 1 else "NEGATIVE" if sentiment_score < -1 else "NEUTRAL"
    spx = snapshot["DATA"].get("SPX", {})
    ndq = snapshot["DATA"].get("NASDAQ", {})
    vix = snapshot["DATA"].get("VIX", {})
    price_action = f"NIFTY {snapshot['DATA'].get('NIFTY',{}).get('CHANGE_PCT','')}% | BANKNIFTY {snapshot['DATA'].get('BANKNIFTY',{}).get('CHANGE_PCT','')}%"
    global_mood = f"SPX {spx.get('CHANGE_PCT','')}% | NDQ {ndq.get('CHANGE_PCT','')}% | VIX {vix.get('CHANGE_PCT','')}%"
    return {
        "TIMESTAMP": datetime.now(IST).isoformat(),
        "NIFTY_MOOD": snapshot["NIFTY_MOOD"],
        "BANKNIFTY_MOOD": snapshot["BANKNIFTY_MOOD"],
        "RETAIL_SENTIMENT": snapshot["RETAIL_SENTIMENT"],
        "NEWS_SENTIMENT": sentiment_label,
        "NEWS_SENTIMENT_SCORE": sentiment_score,
        "PRICE_ACTION_MOVEMENT": price_action,
        "GLOBAL_MARKET_MOOD": global_mood,
        "INDIA_NEWS_1": india_news[0] if len(india_news)>0 else "",
        "INDIA_NEWS_2": india_news[1] if len(india_news)>1 else "",
        "INDIA_NEWS_3": india_news[2] if len(india_news)>2 else "",
        "GLOBAL_NEWS_1": global_news[0] if len(global_news)>0 else "",
        "GLOBAL_NEWS_2": global_news[1] if len(global_news)>1 else "",
        "GLOBAL_NEWS_3": global_news[2] if len(global_news)>2 else "",
        "EARNINGS_NEWS_1": earnings_news[0] if len(earnings_news)>0 else "",
        "EARNINGS_NEWS_2": earnings_news[1] if len(earnings_news)>1 else "",
        "SECTOR_NEWS_1": sector_news[0] if len(sector_news)>0 else "",
        "SECTOR_NEWS_2": sector_news[1] if len(sector_news)>1 else "",
    }


def write_market_intelligence(sh, row: Dict):
    ws = ensure_sheet(sh, MARKET_INTELLIGENCE_SHEET)
    headers = list(row.keys())
    write_table(ws, headers, [row])


def ensure_trading_journal(sh):
    ws = ensure_sheet(sh, JOURNAL_SHEET)
    if ws.get_all_values():
        return
    headers = ["DATE","SYMBOL","MODE","SETUP_NAME","BIAS","ENTRY_PLAN","SL_PLAN","T1","T2","T3","ACTUAL_ENTRY","ACTUAL_EXIT","QTY","RISK_PER_TRADE","PNL","RESULT","MISTAKE","LESSON","EMOTION","NOTES"]
    ws.update("A1", [headers])


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
    headers = ["SYMBOL","TYPE","MODE","CONFIG_STRATEGY","EMA_LAST_SIGNAL","EMA_LAST_ACTION","DMA_LAST_SIGNAL","DMA_LAST_ACTION","LAST_UPDATED"]
    write_table(ws, headers, rows)


def load_telegram_state(sh) -> Dict[str, Dict]:
    ws = ensure_sheet(sh, TELEGRAM_STATE_SHEET)
    rows = ws.get_all_records()
    return {str(r.get("SUMMARY_SLOT", "")): r for r in rows if str(r.get("SUMMARY_SLOT", ""))}


def write_telegram_state(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, TELEGRAM_STATE_SHEET)
    headers = ["SUMMARY_SLOT","SUMMARY_DATE","LAST_SENT_AT"]
    write_table(ws, headers, rows)


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception:
        pass


def build_top_table(rows: List[Dict], limit: int = 5) -> str:
    ranked = sorted(rows, key=lambda x: x.get("SIGNAL_SCORE", 0), reverse=True)[:limit]
    if not ranked:
        return "No ranked setups"
    lines = ["SYMBOL | ACT | SCORE | ENTRY | SL | T1 | OPT"]
    for r in ranked:
        act = r.get("EMA_ACTION") if r.get("EMA_ACTION") in ("BUY", "SELL") else r.get("DMA_ACTION")
        lines.append(f"{r['SYMBOL']} | {act} | {r['SIGNAL_SCORE']} | {r.get('EMA_ENTRY','')} | {r.get('EMA_SL','')} | {r.get('EMA_TARGET_1','')} | {r.get('OPTION_SIDE','')}-{r.get('OPTION_SUGGESTED_STRIKE','')}")
    return "\n".join(lines)


def build_summary_message(slot: str, mi: Dict, rows: List[Dict], limit: int) -> str:
    news_lines = [x for x in [mi.get("INDIA_NEWS_1",""), mi.get("GLOBAL_NEWS_1",""), mi.get("EARNINGS_NEWS_1",""), mi.get("SECTOR_NEWS_1","")] if x]
    news_block = "\n".join([f"- {x}" for x in news_lines[:4]]) if news_lines else "- No fresh news headlines"
    msg = (
        f"[{slot} IST] Market Summary\n"
        f"NIFTY mood: {mi.get('NIFTY_MOOD','')} | BANKNIFTY mood: {mi.get('BANKNIFTY_MOOD','')}\n"
        f"Retail sentiment: {mi.get('RETAIL_SENTIMENT','')} | News sentiment: {mi.get('NEWS_SENTIMENT','')} ({mi.get('NEWS_SENTIMENT_SCORE','')})\n"
        f"Price action: {mi.get('PRICE_ACTION_MOVEMENT','')}\n"
        f"Global mood: {mi.get('GLOBAL_MARKET_MOOD','')}\n\n"
        f"Top setups\n{build_top_table(rows, limit)}\n\n"
        f"Key news\n{news_block}"
    )
    return msg


def eligible_summary_slot(now: datetime) -> str:
    hhmm = now.strftime("%H:%M")
    for slot in SUMMARY_TIMES:
        if hhmm >= slot:
            chosen = slot
    return locals().get('chosen', '')


def should_send_summary(slot: str, tg_state: Dict[str, Dict], today: str) -> bool:
    if not slot:
        return False
    prev = tg_state.get(slot, {})
    return str(prev.get("SUMMARY_DATE", "")) != today


def write_live_signals(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, LIVE_SHEET)
    headers = [
        "TIMESTAMP","SYMBOL","TYPE","MODE","CONFIG_STRATEGY","NOTES","UNDERLYING_FOR_OPTIONS","MAX_RISK_MODE","STRIKE_OFFSET_STEPS",
        "EMA_LAST_PRICE","EMA_CHANGE_PCT","EMA9","EMA21","EMA50","EMA_RECENT_HIGH","EMA_RECENT_LOW","EMA_ATR","EMA_BIAS","EMA_ACTION","EMA_SIGNAL","EMA_ENTRY","EMA_SL","EMA_TARGET_1","EMA_TARGET_2","EMA_TARGET_3","EMA_RR",
        "DMA50","DMA100","DMA200","DMA_SIGNAL","DMA_ACTION","DMA_ENTRY","DMA_SL","DMA_TARGET_1","DMA_TARGET_2","DMA_TARGET_3","DMA_RR","DMA_STATUS",
        "SUPPORT_1","SUPPORT_2","SUPPORT_3","RESISTANCE_1","RESISTANCE_2","RESISTANCE_3","PIVOT",
        "OPTION_UNDERLYING","OPTION_SIDE","OPTION_BASE_STRIKE","OPTION_SUGGESTED_STRIKE","OPTION_STRIKE_STEP","OPTION_MAX_RISK_MODE","OPTION_STRIKE_OFFSET_STEPS","OPTION_RISK_POINTS",
        "SIGNAL_SCORE","STATUS"
    ]
    write_table(ws, headers, rows)


def write_ranked_signals(sh, rows: List[Dict]):
    ws = ensure_sheet(sh, RANKED_SHEET)
    ranked = sorted(rows, key=lambda x: x.get("SIGNAL_SCORE", 0), reverse=True)
    headers = ["TIMESTAMP","SYMBOL","TYPE","MODE","CONFIG_STRATEGY","SIGNAL_SCORE","EMA_BIAS","EMA_ACTION","DMA_ACTION","EMA_ENTRY","EMA_SL","EMA_TARGET_1","EMA_TARGET_2","EMA_TARGET_3","EMA_RR","SUPPORT_1","SUPPORT_2","SUPPORT_3","RESISTANCE_1","RESISTANCE_2","RESISTANCE_3","PIVOT","OPTION_SIDE","OPTION_SUGGESTED_STRIKE","OPTION_RISK_POINTS","STATUS"]
    write_table(ws, headers, ranked)


def main():
    sh = open_sheet()
    cfg_rows = load_config(sh)
    state_map = load_state_map(sh)
    tg_state = load_telegram_state(sh)
    now = datetime.now(IST)
    now_iso = now.isoformat()
    today = now.strftime("%Y-%m-%d")

    snapshot = market_snapshot()
    market_intel = build_market_intelligence(snapshot)

    live_rows, state_rows, instant_alerts = [], [], []
    for cfg in cfg_rows:
        symbol, type_, mode, notes, strategy = cfg["SYMBOL"], cfg["TYPE"], cfg["MODE"], cfg["NOTES"], cfg["STRATEGY"]
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
            row = {
                "TIMESTAMP": now_iso, "SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "NOTES": notes,
                "UNDERLYING_FOR_OPTIONS": cfg["UNDERLYING_FOR_OPTIONS"], "MAX_RISK_MODE": cfg["MAX_RISK_MODE"], "STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"],
                **ema_plan, **dma_plan, **sr_plan, **options_plan,
            }
            row["SIGNAL_SCORE"] = compute_signal_score(row)
            prev_ema_signal = str(prev.get("EMA_LAST_SIGNAL", "")).strip().upper()
            prev_ema_action = str(prev.get("EMA_LAST_ACTION", "")).strip().upper()
            prev_dma_signal = str(prev.get("DMA_LAST_SIGNAL", "")).strip().upper()
            prev_dma_action = str(prev.get("DMA_LAST_ACTION", "")).strip().upper()
            status_parts = []
            if run_ema and (prev_ema_signal != str(ema_plan.get("EMA_SIGNAL", "")).upper() or prev_ema_action != str(ema_plan.get("EMA_ACTION", "")).upper()): status_parts.append("EMA_NEW_SIGNAL")
            if run_dma and (prev_dma_signal != str(dma_plan.get("DMA_SIGNAL", "")).upper() or prev_dma_action != str(dma_plan.get("DMA_ACTION", "")).upper()): status_parts.append("DMA_NEW_SIGNAL")
            if not status_parts: status_parts.append("UNCHANGED")
            row["STATUS"] = " | ".join(status_parts)
            live_rows.append(row)
            if row["SIGNAL_SCORE"] >= 35 and (row.get("EMA_ACTION") in ("BUY","SELL") or row.get("DMA_ACTION") in ("BUY","SELL")) and "UNCHANGED" not in row["STATUS"]:
                instant_alerts.append(f"{row['SYMBOL']} | {row['MODE']} | Score {row['SIGNAL_SCORE']} | {row.get('EMA_ACTION')} {row.get('DMA_ACTION')} | Entry {row.get('EMA_ENTRY')} | SL {row.get('EMA_SL')} | T1 {row.get('EMA_TARGET_1')} | Opt {row.get('OPTION_SIDE')}-{row.get('OPTION_SUGGESTED_STRIKE')}")
            state_rows.append({"SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "EMA_LAST_SIGNAL": ema_plan.get("EMA_SIGNAL", ""), "EMA_LAST_ACTION": ema_plan.get("EMA_ACTION", ""), "DMA_LAST_SIGNAL": dma_plan.get("DMA_SIGNAL", ""), "DMA_LAST_ACTION": dma_plan.get("DMA_ACTION", ""), "LAST_UPDATED": now_iso})
        except Exception as e:
            live_rows.append({"TIMESTAMP": now_iso, "SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "NOTES": notes, "UNDERLYING_FOR_OPTIONS": cfg["UNDERLYING_FOR_OPTIONS"], "MAX_RISK_MODE": cfg["MAX_RISK_MODE"], "STRIKE_OFFSET_STEPS": cfg["STRIKE_OFFSET_STEPS"], "STATUS": f"ERROR: {e}", "SIGNAL_SCORE": 0})
            state_rows.append({"SYMBOL": symbol, "TYPE": type_, "MODE": mode, "CONFIG_STRATEGY": strategy, "EMA_LAST_SIGNAL": "ERROR", "EMA_LAST_ACTION": "ERROR", "DMA_LAST_SIGNAL": "ERROR", "DMA_LAST_ACTION": "ERROR", "LAST_UPDATED": now_iso})

    write_live_signals(sh, live_rows)
    write_ranked_signals(sh, live_rows)
    write_state_map(sh, state_rows)
    ensure_trading_journal(sh)
    write_market_intelligence(sh, market_intel)

    if instant_alerts:
        send_telegram("Immediate alerts\n" + "\n".join(instant_alerts[:10]))

    slot = eligible_summary_slot(now)
    if should_send_summary(slot, tg_state, today):
        limit = 10 if slot == "20:30" else 5
        send_telegram(build_summary_message(slot, market_intel, live_rows, limit))
        tg_state[slot] = {"SUMMARY_SLOT": slot, "SUMMARY_DATE": today, "LAST_SENT_AT": now_iso}
        write_telegram_state(sh, list(tg_state.values()))


if __name__ == "__main__":
    main()
