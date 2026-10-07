"""יקום המניות של הבוט: S&P 500 + נאסד"ק 100 + מניות מובילות בת"א.

הרשימה נמשכת מוויקיפדיה ומתעדכנת פעם בשבוע. אם המשיכה נכשלת, משתמשים
ברשימה האחרונה שנשמרה, ואם אין, ברשימת הבסיס מ-config.py.
"""
import datetime as dt
import io
import json
import os
import pandas as pd
import requests
import yfinance as yf
from config import WATCHLIST_TA, WATCHLIST_US, UNIVERSE_MODE

CACHE = "logs/universe.json"
UA = {"User-Agent": "Mozilla/5.0 (trading-bot; personal use)"}

# מניות ת"א נזילות. סימבול שלא קיים ב-Yahoo פשוט מדולג.
TA_EXTRA = [
    "TEVA", "LUMI", "POLI", "DSCT", "MZTF", "FIBI", "NICE", "ESLT", "ICL", "TSEM", "NVMI",
    "BEZQ", "AZRG", "MLSR", "ALHE", "AMOT", "BIG", "SPEN", "ORL", "DLEKG", "NWMD", "HARL",
    "CLIS", "PHOE", "MGDL", "ENLT", "ENRG", "OPCE", "SAE", "STRS", "CAMT", "ONE", "MTRX",
    "FORTY", "ELCO", "ELTR", "ISCD", "GCT", "MVNE", "ISRA", "ASHG", "DELG", "FOX", "RMLI",
    "SKBN", "AURA", "ARPT", "KEN", "NXSN", "PZOL", "MMHD", "IDIN", "SMT", "ECP", "BLSR",
    "DANE", "EQTL", "TASE", "PTNR", "CEL", "AFCON", "ILCO", "ELAL", "MRIN", "ALMA",
]


# גיבוי אם ויקיפדיה לא זמינה: מניות גדולות ונזילות בארה"ב
US_FALLBACK = """AAPL MSFT NVDA AMZN GOOGL META AVGO TSLA BRK-B JPM LLY V UNH XOM MA COST HD PG JNJ ABBV
WMT NFLX BAC CRM ORCL CVX KO MRK AMD PEP ADBE TMO LIN ACN MCD CSCO ABT WFC DIS INTU QCOM TXN IBM GE
AMAT CAT VZ PFE NOW AMGN ISRG UBER PM GS SPGI RTX CMCSA UNP NEE HON LOW BKNG T BLK AXP SYK ELV PLD
LRCX MU PGR TJX VRTX MDT ADI SCHW C BSX DE PANW KLAC MMC ETN CB REGN GILD LMT ADP SBUX MS ANET SNPS
CDNS CRWD MELI PYPL ABNB MRVL FTNT ORLY COP MO SO DUK""".split()


def _wiki(url, col_names):
    html = requests.get(url, headers=UA, timeout=30).text
    for t in pd.read_html(io.StringIO(html)):
        for c in t.columns:
            if str(c).strip() in col_names:
                vals = t[c].astype(str).str.strip().str.replace(".", "-", regex=False)
                return [v for v in vals if v and v.upper() == v and len(v) <= 6]
    return []


def fetch():
    us = set(WATCHLIST_US) | set(US_FALLBACK)
    try:
        us |= set(_wiki("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", {"Symbol"}))
    except Exception:
        pass
    try:
        us |= set(_wiki("https://en.wikipedia.org/wiki/Nasdaq-100", {"Ticker", "Symbol"}))
    except Exception:
        pass
    ta = set(WATCHLIST_TA) | {f"{s}.TA" for s in TA_EXTRA}
    return sorted(ta), sorted(us)


def get():
    """מחזיר (רשימת ת"א, רשימת ארה"ב)."""
    if UNIVERSE_MODE != "wide":
        return list(WATCHLIST_TA), list(WATCHLIST_US)
    today = dt.date.today().isoformat()
    if os.path.exists(CACHE):
        c = json.load(open(CACHE, encoding="utf-8"))
        age = (dt.date.today() - dt.date.fromisoformat(c["date"])).days
        if age < 7 and len(c["US"]) > 300:
            return c["TA"], c["US"]
    ta, us = fetch()
    if len(us) > 300:
        os.makedirs("logs", exist_ok=True)
        json.dump({"date": today, "TA": ta, "US": us}, open(CACHE, "w", encoding="utf-8"), indent=0)
        return ta, us
    if os.path.exists(CACHE):                      # המשיכה נכשלה: הרשימה האחרונה
        c = json.load(open(CACHE, encoding="utf-8"))
        return c["TA"], c["US"]
    return ta, us


def bulk_load(symbols, period="2y", chunk=100):
    """הורדה מרוכזת של נרות יומיים. מחזיר {סימבול: DataFrame או None}."""
    out = {}
    for k in range(0, len(symbols), chunk):
        part = symbols[k:k + chunk]
        try:
            raw = yf.download(part, period=period, auto_adjust=True, group_by="ticker",
                              threads=True, progress=False)
        except Exception:
            raw = None
        for s in part:
            df = None
            try:
                if raw is not None and s in raw.columns.get_level_values(0):
                    df = raw[s][["Open", "High", "Low", "Close", "Volume"]].dropna()
                    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                    if len(df) < 220:
                        df = None
            except Exception:
                df = None
            out[s] = df
    return out
