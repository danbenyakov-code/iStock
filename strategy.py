"""לוגיקת האסטרטגיה. משותפת לבוט היומי ולבדיקה ההיסטורית."""
import pandas as pd
import yfinance as yf
from config import *


def market_of(symbol):
    return "TA" if symbol.endswith(".TA") else "US"


def load(symbol, period="2y"):
    """מוריד נרות יומיים. מחזיר None אם אין נתונים."""
    try:
        df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
    except Exception:
        return None
    if df is None or df.empty or len(df) < 220:
        return None
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def usd_ils_rate():
    try:
        h = yf.Ticker("ILS=X").history(period="5d")
        return float(h["Close"].iloc[-1]), True
    except Exception:
        return 3.7, False


def to_ils(symbol, price, usdils):
    # מחירי ת"א ב-Yahoo הם באגורות
    return price / 100 if market_of(symbol) == "TA" else price * usdils


def add_indicators(df):
    d = df.copy()
    c = d["Close"]
    d["sma50"] = c.rolling(50).mean()
    d["sma200"] = c.rolling(200).mean()
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + gain / loss)
    prev_c = c.shift(1)
    tr = pd.concat([d["High"] - d["Low"],
                    (d["High"] - prev_c).abs(),
                    (d["Low"] - prev_c).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["high20"] = d["High"].rolling(20).max().shift(1)
    d["vol20"] = d["Volume"].rolling(20).mean().shift(1)
    return d


def market_ok_series(index_df):
    d = add_indicators(index_df)
    return d["Close"] > d["sma200"]


def entry_signal(d, i, earnings_reaction_days=None):
    """מחזיר 'post_earnings', 'pullback', 'breakout' או None ליום i."""
    if i < 1:
        return None
    row, prev = d.iloc[i], d.iloc[i - 1]
    if pd.isna(row["sma200"]) or pd.isna(prev["rsi"]):
        return None
    # אסטרטגיה 3: תגובה חזקה לדוח (Post-Earnings Drift)
    if (USE_POST_EARNINGS and earnings_reaction_days and d.index[i] in earnings_reaction_days
            and row["Open"] > prev["Close"] * 1.04
            and row["vol20"] > 0 and row["Volume"] > 2 * row["vol20"]
            and row["Close"] > (row["High"] + row["Low"]) / 2
            and row["Close"] > row["sma200"]):
        return "post_earnings"
    uptrend = row["Close"] > row["sma200"] and row["sma50"] > row["sma200"]
    if uptrend and prev["rsi"] < 40 <= row["rsi"]:
        return "pullback"
    if (row["Close"] > row["sma200"] and row["Close"] > row["high20"]
            and row["vol20"] > 0 and row["Volume"] > 1.5 * row["vol20"]):
        return "breakout"
    return None


def initial_stop(d, i):
    return d["Close"].iloc[i] - ATR_STOP_MULT * d["atr"].iloc[i]


def exit_check(d, entry_i, i, entry_price, init_stop):
    """בודק יציאה לפי סגירה. מחזיר (סיבה או None, סטופ נוכחי)."""
    highest = d["Close"].iloc[entry_i:i + 1].max()
    stop = max(init_stop, highest - ATR_TRAIL_MULT * d["atr"].iloc[i])
    close = d["Close"].iloc[i]
    if close < stop:
        return "stop", stop
    r = entry_price - init_stop
    if i - entry_i >= TIME_STOP_DAYS and close < entry_price + r:
        return "time", stop
    return None, stop


def position_size(entry_ils, stop_ils, capital=CAPITAL_ILS):
    per_share = entry_ils - stop_ils
    if per_share <= 0:
        return 0
    by_risk = int(capital * RISK_PER_TRADE / per_share)
    by_cap = int(capital * MAX_POSITION_PCT / entry_ils)
    return max(0, min(by_risk, by_cap))
