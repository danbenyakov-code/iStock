"""שכבות מידע: דוחות כספיים, איכות פונדמנטלית, חוזק יחסי ו-Reddit."""
import pandas as pd
import requests
import yfinance as yf
from config import *
from strategy import market_of

_info, _earn = {}, {}


# ---------- דוחות כספיים ----------
def earnings_dates(sym, limit=60):
    """תאריכי דוחות, עבר ועתיד. רשימה ריקה אם אין נתונים."""
    if sym in _earn:
        return _earn[sym]
    out = []
    try:
        df = yf.Ticker(sym).get_earnings_dates(limit=limit)
        if df is not None and len(df):
            for ts in df.index:
                ts = pd.Timestamp(ts)
                if ts.tzinfo is not None:
                    ts = ts.tz_convert("America/New_York").tz_localize(None)
                out.append(ts)
    except Exception:
        pass
    if not out:
        try:
            cal = yf.Ticker(sym).calendar or {}
            out = [pd.Timestamp(x) for x in cal.get("Earnings Date", [])]
        except Exception:
            pass
    _earn[sym] = sorted(set(out))
    return _earn[sym]


def reaction_days(dates, index):
    """יום המסחר שבו השוק מגיב לדוח (דוח אחרי הצהריים = התגובה למחרת)."""
    days = set()
    for ts in dates:
        side = "right" if ts.hour >= 12 else "left"
        pos = index.searchsorted(ts.normalize(), side=side)
        if pos < len(index):
            days.add(index[pos])
    return days


def days_to_next_earnings(dates, today):
    future = [d for d in dates if d.normalize() >= today]
    return (min(future).normalize() - today).days if future else None


def earnings_soon(dates, day, window):
    """האם יש דוח בחלון של window ימים אחרי day (לבדיקה היסטורית)."""
    end = day + pd.Timedelta(days=window)
    return any(day < d.normalize() <= end for d in dates)


# ---------- איכות פונדמנטלית ----------
def quality(sym):
    if sym in _info:
        return _info[sym]
    try:
        info = yf.Ticker(sym).info or {}
    except Exception:
        info = {}
    sector = info.get("sector") or "לא ידוע"
    checks = []

    def chk(name, val, rule):
        if isinstance(val, (int, float)):
            checks.append((name, bool(rule(val))))

    chk("רווחיות", info.get("profitMargins"), lambda v: v > 0)
    chk("תשואה להון", info.get("returnOnEquity"), lambda v: v > 0.05)
    chk("צמיחת הכנסות", info.get("revenueGrowth"), lambda v: v > -0.10)
    if sector not in ("Financial Services", "Real Estate"):   # בבנקים יחס חוב לא רלוונטי
        chk("חוב", info.get("debtToEquity"), lambda v: v < 200)
    failed = [n for n, ok in checks if not ok]
    _info[sym] = {"sector": sector, "n": len(checks), "passed": len(checks) - len(failed),
                  "failed": failed, "ok": not failed}
    return _info[sym]


def quality_text(q):
    if q["n"] == 0:
        return "איכות: אין נתונים"
    if q["ok"]:
        return f"איכות: ✅ {q['passed']}/{q['n']}"
    return f"איכות: ❌ נכשל ב{', '.join(q['failed'])}"


# ---------- חוזק יחסי ----------
def relative_strength(d, index_df, n=RS_LOOKBACK):
    """תשואת המניה פחות תשואת המדד ב-n ימים, באחוזים."""
    if index_df is None or len(d) <= n:
        return 0.0
    idx = index_df["Close"].reindex(d.index, method="ffill")
    s = d["Close"].iloc[-1] / d["Close"].iloc[-1 - n] - 1
    m = idx.iloc[-1] / idx.iloc[-1 - n] - 1
    return float((s - m) * 100) if pd.notna(m) else float(s * 100)


# ---------- Reddit ----------
def reddit_map():
    out = {}
    if not USE_REDDIT:
        return out
    try:
        for page in (1, 2, 3):
            r = requests.get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}", timeout=15)
            for x in r.json().get("results", []):
                out[str(x.get("ticker", "")).upper()] = x
    except Exception:
        pass
    return out


def reddit_note(sym, rmap):
    if market_of(sym) != "US" or sym not in rmap:
        return None
    x = rmap[sym]
    m, m0 = int(x.get("mentions") or 0), int(x.get("mentions_24h_ago") or 0)
    txt = f"💬 Reddit: מקום {x.get('rank')} | {m} אזכורים (יום קודם: {m0})"
    if m >= 50 and m >= 3 * max(m0, 1):
        txt += "\n   ⚠️ קפיצה חדה בדיבור ברשת: סיכון לתנודתיות ולשיא רגעי"
    return txt
