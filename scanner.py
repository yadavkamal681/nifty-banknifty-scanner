import os
import json
from datetime import datetime
from typing import Dict, List, Tuple

import requests
import pytz
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf

# ========== BASIC CONFIG ==========

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
CONFIG_SHEET = "CONFIG"
LIVE_SHEET = "LIVE_SIGNALS"
STATE_SHEET = "STATE"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

IST = pytz.timezone("Asia/Kolkata")

# Index strike steps and lot sizes (approx – adjust if you want)
INDEX_STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
INDEX_LOT_SIZE = {"NIFTY": 25, "BANKNIFTY": 15, "SENSEX": 10}
INDEX_MIN_R = {"NIFTY": 10, "BANKNIFTY": 30, "SENSEX": 50}  # min risk in points
STOCK_MIN_R_PCT = 0.005  # 0.5% minimum risk for stocks


# ========== HELPERS: IDENTIFIERS & SHEETS ==========

def state_key(symbol: str, mode: str) -> str:
    return f"{symbol}|{mode}"


def to_yahoo_ticker(symbol: str, type_: str) -> str:
    """
    Map our symbols to Yahoo Finance tickers.

    - NSE stocks: SYMBOL.NS  (e.g. RELIANCE -> RELIANCE.NS)
    - Indices:
      NIFTY     -> ^NSEI
      BANKNIFTY -> ^NSEBANK
      SENSEX    -> ^BSESN
    """
    symbol = symbol.upper()
    type_ = type_.upper()
    if type_ == "INDEX":
        if symbol == "NIFTY":
            return "^NSEI"
        if symbol == "BANKNIFTY":
            return "^NSEBANK"
        if symbol == "SENSEX":
            return "^BSESN"
    return f"{symbol}.NS"


def get_gspread_client():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")
    sa_info = json.loads(sa_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(sa_info, scopes=scopes)
    return gspread.authorize(credentials)


def open_sheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID not set")
    gc = get_gspread_client()
    return gc.open_by_key(SPREADSHEET_ID)


def load_config_from_sheet(sh) -> List[Dict]:
    ws = sh.worksheet(CONFIG_SHEET)
    rows = ws.get_all_records()
    configs: List[Dict] = []
    for row in rows:
        if str(row.get("ACTIVE", "")).strip().upper() != "TRUE":
            continue
        cfg = {
            "SYMBOL": str(row.get("SYMBOL", "")).strip().upper(),
            "TYPE": str(row.get("TYPE", "")).strip().upper(),
            "MODE": str(row.get("MODE", "INTRADAY")).strip().upper(),
            "UNDERLYING_FOR_OPTIONS": str(row.get("UNDERLYING_FOR_OPTIONS", "")).strip().upper(),
            "MAX_RISK_MODE": str(row.get("MAX_RISK_MODE", "NORMAL")).strip().upper(),
            "STRIKE_OFFSET_STEPS": int(row.get("STRIKE_OFFSET_STEPS", 1) or 1),
            "NOTES": row.get("NOTES", ""),
        }
        configs.append(cfg)
    return configs


def empty_state(symbol: str, mode: str) -> Dict:
    return {
        "SYMBOL": symbol,
        "TYPE": "",
        "MODE": mode,
        "LAST_STRIKE": "",
        "LAST_EXPIRY": "",
        "LAST_CE_PE": "",
        "LAST_SIGNAL": "NO_SIGNAL",
        "LAST_ENTRY_ZONE_LOW": "",
        "LAST_ENTRY_ZONE_HIGH": "",
        "LAST_SL": "",
        "LAST_T1": "",
        "LAST_T2": "",
        "LAST_LTP": "",
        "LAST_UPDATED": "",
    }


def load_state_from_sheet(sh) -> Dict[str, Dict]:
    try:
        ws = sh.worksheet(STATE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        return {}
    rows = ws.get_all_records()
    state_map: Dict[str, Dict] = {}
    for row in rows:
        symbol = str(row.get("SYMBOL", "")).strip().upper()
        mode = str(row.get("MODE", "INTRADAY")).strip().upper()
        key = state_key(symbol, mode)
        state_map[key] = {
            "SYMBOL": symbol,
            "TYPE": str(row.get("TYPE", "")).strip().upper(),
            "MODE": mode,
            "LAST_STRIKE": str(row.get("LAST_STRIKE", "")).strip().upper(),
            "LAST_EXPIRY": str(row.get("LAST_EXPIRY", "")).strip(),
            "LAST_CE_PE": str(row.get("LAST_CE_PE", "")).strip().upper(),
            "LAST_SIGNAL": str(row.get("LAST_SIGNAL", "NO_SIGNAL")).strip().upper(),
            "LAST_ENTRY_ZONE_LOW": row.get("LAST_ENTRY_ZONE_LOW", ""),
            "LAST_ENTRY_ZONE_HIGH": row.get("LAST_ENTRY_ZONE_HIGH", ""),
            "LAST_SL": row.get("LAST_SL", ""),
            "LAST_T1": row.get("LAST_T1", ""),
            "LAST_T2": row.get("LAST_T2", ""),
            "LAST_LTP": row.get("LAST_LTP", ""),
            "LAST_UPDATED": row.get("LAST_UPDATED", ""),
        }
    return state_map


def write_live_signals_to_sheet(sh, live_rows: List[Dict]):
    try:
        ws = sh.worksheet(LIVE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=LIVE_SHEET, rows="1000", cols="20")

    headers = [
        "TIMESTAMP", "SYMBOL", "TYPE", "MODE", "DIRECTION",
        "STRIKE", "EXPIRY", "CE_PE", "SIGNAL",
        "ENTRY_ZONE_LOW", "ENTRY_ZONE_HIGH",
        "SL", "T1", "T2", "LTP",
        "REASON", "RISK_PER_LOT", "COMMENT",
    ]

    values = [headers]
    for row in live_rows:
        values.append([row.get(h, "") for h in headers])

    ws.clear()
    ws.update("A1", values)


def write_state_to_sheet(sh, state_map: Dict[str, Dict]):
    try:
        ws = sh.worksheet(STATE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=STATE_SHEET, rows="1000", cols="20")

    headers = [
        "SYMBOL", "TYPE", "MODE",
        "LAST_STRIKE", "LAST_EXPIRY", "LAST_CE_PE",
        "LAST_SIGNAL",
        "LAST_ENTRY_ZONE_LOW", "LAST_ENTRY_ZONE_HIGH",
        "LAST_SL", "LAST_T1", "LAST_T2",
        "LAST_LTP", "LAST_UPDATED",
    ]

    values = [headers]
    for st in state_map.values():
        values.append([st.get(h, "") for h in headers])

    ws.clear()
    ws.update("A1", values)


# ========== DATA FETCH: yfinance (NSE) ==========

def fetch_index_data(symbols: List[str]) -> Dict[str, Dict]:
    """
    Fetch 5-min OHLC and indicators for each index symbol using yfinance.
    """
    results: Dict[str, Dict] = {}
    now_ist = datetime.now(IST)
    today_date = now_ist.date()

    for symbol in symbols:
        yticker = to_yahoo_ticker(symbol, "INDEX")

        df = yf.download(
            yticker,
            period="2d",
            interval="5m",
            auto_adjust=True,
            progress=False,
        )
        if df.empty:
            continue

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)

        df_today = df[df.index.date == today_date]
        if df_today.empty:
            last_date = df.index.date[-1]
            df_today = df[df.index.date == last_date]

        df_daily = yf.download(
            yticker,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        PDH = PDL = float(df_today["Close"].iloc[-1])
        if len(df_daily) >= 2:
            last_completed = df_daily.iloc[-2]
            PDH = float(last_completed["High"])
            PDL = float(last_completed["Low"])

        ohlc_today = df_today[["Open", "High", "Low", "Close"]].rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"}
        )

        ema10 = ohlc_today["close"].ewm(span=10, adjust=False).mean()
        ema20 = ohlc_today["close"].ewm(span=20, adjust=False).mean()

        if "Volume" in df_today.columns:
            pv = df_today["Close"] * df_today["Volume"]
            vwap_today = pv.cumsum() / df_today["Volume"].cumsum()
            VWAP = float(vwap_today.iloc[-1])
        else:
            VWAP = float(ohlc_today["close"].iloc[-1])

        high = ohlc_today["high"]
        low = ohlc_today["low"]
        close = ohlc_today["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = tr.rolling(window=14, min_periods=1).mean()
        ATR14 = float(atr14.iloc[-1])

        orh = orl = None
        if len(ohlc_today) >= 3:
            first3 = ohlc_today.iloc[:3]
            orh = float(first3["high"].max())
            orl = float(first3["low"].min())

        results[symbol] = {
            "ohlc_window": ohlc_today,
            "indicators": {
                "EMA10": float(ema10.iloc[-1]),
                "EMA20": float(ema20.iloc[-1]),
                "VWAP": VWAP,
                "ATR14": ATR14,
                "PDH": PDH,
                "PDL": PDL,
                "ORH": orh,
                "ORL": orl,
            },
        }

    return results


def fetch_stock_data(symbols: List[str]) -> Dict[str, Dict]:
    """
    Fetch 5-min OHLC and indicators for each stock symbol using yfinance.
    """
    results: Dict[str, Dict] = {}
    now_ist = datetime.now(IST)
    today_date = now_ist.date()

    for symbol in symbols:
        yticker = to_yahoo_ticker(symbol, "STOCK")

        df = yf.download(
            yticker,
            period="2d",
            interval="5m",
            auto_adjust=True,
            progress=False,
        )
        if df.empty:
            continue

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(IST)
        else:
            df.index = df.index.tz_convert(IST)

        df_today = df[df.index.date == today_date]
        if df_today.empty:
            last_date = df.index.date[-1]
            df_today = df[df.index.date == last_date]

        df_daily = yf.download(
            yticker,
            period="5d",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        PDH = PDL = float(df_today["Close"].iloc[-1])
        if len(df_daily) >= 2:
            last_completed = df_daily.iloc[-2]
            PDH = float(last_completed["High"])
            PDL = float(last_completed["Low"])

        ohlc_today = df_today[["Open", "High", "Low", "Close"]].rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"}
        )

        ema10 = ohlc_today["close"].ewm(span=10, adjust=False).mean()
        ema20 = ohlc_today["close"].ewm(span=20, adjust=False).mean()

        if "Volume" in df_today.columns:
            pv = df_today["Close"] * df_today["Volume"]
            vwap_today = pv.cumsum() / df_today["Volume"].cumsum()
            VWAP = float(vwap_today.iloc[-1])
        else:
            VWAP = float(ohlc_today["close"].iloc[-1])

        high = ohlc_today["high"]
        low = ohlc_today["low"]
        close = ohlc_today["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = tr.rolling(window=14, min_periods=1).mean()
        ATR14 = float(atr14.iloc[-1])

        orh = orl = None
        if len(ohlc_today) >= 3:
            first3 = ohlc_today.iloc[:3]
            orh = float(first3["high"].max())
            orl = float(first3["low"].min())

        results[symbol] = {
            "ohlc_window": ohlc_today,
            "indicators": {
                "EMA10": float(ema10.iloc[-1]),
                "EMA20": float(ema20.iloc[-1]),
                "VWAP": VWAP,
                "ATR14": ATR14,
                "PDH": PDH,
                "PDL": PDL,
                "ORH": orh,
                "ORL": orl,
            },
        }

    return results


def fetch_option_chain(symbol: str) -> Dict[str, Dict]:
    """
    Fetch nearest-expiry option chain for an index via yfinance.
    Returns: { '24000CE': {'ltp': ...}, '24000PE': {...}, ... }
    """
    yticker = to_yahoo_ticker(symbol, "INDEX")
    tk = yf.Ticker(yticker)

    try:
        expiries = tk.options
    except Exception:
        return {}
    if not expiries:
        return {}

    nearest = expiries[0]
    try:
        chain = tk.option_chain(nearest)
    except Exception:
        return {}

    calls = getattr(chain, "calls", None)
    puts = getattr(chain, "puts", None)

    result: Dict[str, Dict] = {}

    if calls is not None and not calls.empty:
        for _, row in calls.iterrows():
            strike = row.get("strike")
            ltp = row.get("lastPrice")
            if pd.isna(strike) or pd.isna(ltp):
                continue
            key = f"{int(round(strike))}CE"
            result[key] = {"ltp": float(ltp)}

    if puts is not None and not puts.empty:
        for _, row in puts.iterrows():
            strike = row.get("strike")
            ltp = row.get("lastPrice")
            if pd.isna(strike) or pd.isna(ltp):
                continue
            key = f"{int(round(strike))}PE"
            result[key] = {"ltp": float(ltp)}

    return result


# ========== DIRECTION HELPERS ==========

def _compute_direction_index(ohlc_window: pd.DataFrame, indicators: Dict) -> str:
    C0 = float(ohlc_window["close"].iloc[-1])
    EMA10 = float(indicators.get("EMA10", C0))
    EMA20 = float(indicators.get("EMA20", C0))
    VWAP = float(indicators.get("VWAP", C0))
    if C0 > VWAP and EMA10 > EMA20 and (EMA10 - EMA20) / C0 >= 0.0005:
        return "UP"
    if C0 < VWAP and EMA10 < EMA20 and (EMA20 - EMA10) / C0 >= 0.0005:
        return "DOWN"
    return "SIDE"


def _compute_direction_stock(ohlc_window: pd.DataFrame, indicators: Dict) -> str:
    C0 = float(ohlc_window["close"].iloc[-1])
    EMA10 = float(indicators.get("EMA10", C0))
    EMA20 = float(indicators.get("EMA20", C0))
    VWAP = float(indicators.get("VWAP", C0))
    if C0 > VWAP and EMA10 > EMA20 and (EMA10 - EMA20) / C0 >= 0.0007:
        return "UP"
    if C0 < VWAP and EMA10 < EMA20 and (EMA20 - EMA10) / C0 >= 0.0007:
        return "DOWN"
    return "SIDE"


# ========== INDEX STRATEGY ==========

def compute_index_signal(
    config: Dict,
    ohlc_window: pd.DataFrame,
    indicators: Dict,
    last_state: Dict,
    option_chain: Dict,
    now_ts: datetime,
) -> Tuple[Dict, Dict, bool, str]:
    symbol = config["SYMBOL"]
    mode = config["MODE"]
    new_state = last_state.copy() if last_state else empty_state(symbol, mode)

    live_row: Dict = {
        "TIMESTAMP": now_ts.isoformat(),
        "SYMBOL": symbol,
        "TYPE": config["TYPE"],
        "MODE": mode,
        "DIRECTION": "SIDE",
        "STRIKE": "",
        "EXPIRY": "",
        "CE_PE": "",
        "SIGNAL": "NO_SIGNAL",
        "ENTRY_ZONE_LOW": "",
        "ENTRY_ZONE_HIGH": "",
        "SL": "",
        "T1": "",
        "T2": "",
        "LTP": "",
        "REASON": "",
        "RISK_PER_LOT": "",
        "COMMENT": "",
    }

    alert_required = False
    alert_message = ""

    t = now_ts.astimezone(IST).time()

    direction = _compute_direction_index(ohlc_window, indicators)
    live_row["DIRECTION"] = direction

    C0 = float(ohlc_window["close"].iloc[-1])
    C1 = float(ohlc_window["close"].iloc[-2]) if len(ohlc_window) >= 2 else C0
    H0 = float(ohlc_window["high"].iloc[-1])
    L0 = float(ohlc_window["low"].iloc[-1])
    H_prev = float(ohlc_window["high"].iloc[-2]) if len(ohlc_window) >= 2 else H0
    L_prev = float(ohlc_window["low"].iloc[-2]) if len(ohlc_window) >= 2 else L0

    EMA10 = float(indicators.get("EMA10", C0))
    EMA20 = float(indicators.get("EMA20", C0))
    ATR14 = float(indicators.get("ATR14", 0))
    PDH = float(indicators.get("PDH", C0))
    PDL = float(indicators.get("PDL", C0))
    ORH = indicators.get("ORH")
    ORL = indicators.get("ORL")

    if ORH is not None and ORL is not None:
        key_up_level = max(PDH, float(ORH))
        key_down_level = min(PDL, float(ORL))
    else:
        key_up_level = PDH
        key_down_level = PDL

    last_signal = str(new_state.get("LAST_SIGNAL", "NO_SIGNAL")).upper()

    signal = "NO_SIGNAL"
    reason = ""

    if datetime.strptime("09:25", "%H:%M").time() <= t <= datetime.strptime("15:20", "%H:%M").time():
        if direction == "UP" and last_signal not in ("NEW_BUY", "CONTINUE_HOLD"):
            breakout_up = (C1 <= key_up_level) and (C0 > key_up_level * 1.0005)
            pullback_long = ((L0 <= EMA10 or L0 <= EMA20) and (C0 > H_prev))
            if breakout_up or pullback_long:
                signal = "NEW_BUY"
                reason = "Trend up + breakout" if breakout_up else "Trend up + EMA pullback"
        if direction == "DOWN" and last_signal not in ("NEW_SELL", "CONTINUE_HOLD") and signal == "NO_SIGNAL":
            breakout_down = (C1 >= key_down_level) and (C0 < key_down_level * 0.9995)
            pullback_short = ((H0 >= EMA10 or H0 >= EMA20) and (C0 < L_prev))
            if breakout_down or pullback_short:
                signal = "NEW_SELL"
                reason = "Trend down + breakdown" if breakout_down else "Trend down + EMA pullback"

    entry_under = None

    if signal in ("NEW_BUY", "NEW_SELL"):
        entry_under = C0
        R = max(0.5 * ATR14, float(INDEX_MIN_R.get(symbol, 10)))
        # We don't store underlying SL/T1/T2 into sheet, just use for orientation

    strike_symbol = ""
    expiry_str = ""
    ce_pe = ""
    entry_zone_low = ""
    entry_zone_high = ""
    sl_opt = ""
    t1_opt = ""
    t2_opt = ""
    risk_per_lot = ""

    if signal in ("NEW_BUY", "NEW_SELL") and config.get("UNDERLYING_FOR_OPTIONS"):
        step = INDEX_STRIKE_STEP.get(symbol, 50)
        atm = round(entry_under / step) * step
        offset = config.get("STRIKE_OFFSET_STEPS", 1)
        if signal == "NEW_BUY":
            strike_price = atm + offset * step
            ce_pe = "CE"
        else:
            strike_price = atm - offset * step
            ce_pe = "PE"
        strike_symbol = f"{int(strike_price)}{ce_pe}"
        opt = option_chain.get(strike_symbol)
        if opt and "ltp" in opt:
            opt_ltp = float(opt["ltp"])
            entry_zone_low = opt_ltp * 0.97
            entry_zone_high = opt_ltp * 1.02
            sl_opt = opt_ltp * 0.7
            t1_opt = opt_ltp * 1.3
            t2_opt = opt_ltp * 1.6
            lot = INDEX_LOT_SIZE.get(symbol, 1)
            risk_per_lot = (entry_zone_high - sl_opt) * lot
        else:
            signal = "NO_SIGNAL"
            reason = "No option data for strike"

    if signal == "NO_SIGNAL" and last_signal in ("NEW_BUY", "NEW_SELL", "CONTINUE_HOLD") and new_state.get("LAST_STRIKE"):
        strike_symbol = new_state["LAST_STRIKE"]
        ce_pe = new_state.get("LAST_CE_PE", "")
        opt = option_chain.get(strike_symbol)
        if opt and "ltp" in opt:
            opt_ltp = float(opt["ltp"])
            entry_zone_low = new_state.get("LAST_ENTRY_ZONE_LOW")
            entry_zone_high = new_state.get("LAST_ENTRY_ZONE_HIGH")
            sl_opt = new_state.get("LAST_SL")
            t1_opt = new_state.get("LAST_T1")
            t2_opt = new_state.get("LAST_T2")
            exit_signal = False
            exit_reason = ""
            try:
                if sl_opt not in ("", None) and opt_ltp <= float(sl_opt):
                    exit_signal = True
                    exit_reason = "Option SL hit"
            except Exception:
                pass
            if not exit_signal:
                if ce_pe == "CE" and direction != "UP":
                    exit_signal = True
                    exit_reason = "Trend flip against CE"
                elif ce_pe == "PE" and direction != "DOWN":
                    exit_signal = True
                    exit_reason = "Trend flip against PE"
            if __name__ == "__main__":
        main()
             
