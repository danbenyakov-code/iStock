"""סריקה יומית: פוזיציות, כניסות חדשות, ובמצב נייר גם ביצוע ורישום אוטומטי."""
import csv
import datetime as dt
import json
import os
import requests
import pandas as pd
from config import *
from strategy import *
from insights import *

NAMES = {"post_earnings": "תגובה חזקה לדוח כספי", "pullback": "קנייה בתיקון במגמת עלייה", "breakout": "פריצה עם מחזור גבוה"}
LOG = "logs"
P_POS, P_TRADES = f"{LOG}/paper_positions.csv", f"{LOG}/paper_trades.csv"
EVENTS, PENDING = f"{LOG}/events.csv", f"{LOG}/paper_pending.json"
POS_F = ["symbol", "entry_date", "entry_price", "shares", "initial_stop", "type"]
TRADE_F = ["symbol", "type", "entry_date", "entry_price", "exit_date", "exit_price",
           "shares", "reason", "pnl_ils", "pnl_pct", "r_multiple"]
EVENT_F = ["date", "mode", "event", "symbol", "details"]
_cache = {}


def today():
    return pd.Timestamp(dt.datetime.now(dt.timezone.utc).date())


def get(sym):
    if sym not in _cache:
        _cache[sym] = load(sym)
    return _cache[sym]


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("symbol") and not r["symbol"].startswith("#")]


def write_csv(path, fields, rows):
    os.makedirs(LOG, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_event(mode, event, symbol, details):
    os.makedirs(LOG, exist_ok=True)
    new = not os.path.exists(EVENTS)
    with open(EVENTS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_F)
        if new:
            w.writeheader()
        w.writerow({"date": f"{today():%Y-%m-%d}", "mode": mode, "event": event,
                    "symbol": symbol, "details": details})


def parse_pos(r):
    return {"symbol": r["symbol"].strip().upper(), "entry_date": pd.Timestamp(r["entry_date"].strip()),
            "entry_price": float(r["entry_price"]), "shares": int(float(r["shares"])),
            "initial_stop": float(r["initial_stop"]), "type": r.get("type") or ""}


def pos_row(p):
    return {**p, "entry_date": f"{p['entry_date']:%Y-%m-%d}"}


def send(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return
    for k in range(0, len(text), 4000):
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text[k:k + 4000]}, timeout=30)


def fmt(symbol, p):
    return f"{p:,.1f} אג'" if market_of(symbol) == "TA" else f"${p:,.2f}"


def next_open(sym, after_date):
    """מחיר הפתיחה ביום המסחר הראשון אחרי after_date, או None אם עוד אין."""
    df = get(sym)
    if df is None:
        return None, None
    later = df.index[df.index > pd.Timestamp(after_date)]
    return (later[0], float(df.loc[later[0], "Open"])) if len(later) else (None, None)


def close_trade(p, date, price, reason, usdils):
    entry_ils, exit_ils = to_ils(p["symbol"], p["entry_price"], usdils), to_ils(p["symbol"], price, usdils)
    cost = COMMISSION_PCT_ROUNDTRIP * entry_ils * p["shares"]
    pnl = p["shares"] * (exit_ils - entry_ils) - cost
    r = p["entry_price"] - p["initial_stop"]
    return {"symbol": p["symbol"], "type": p["type"], "entry_date": f"{p['entry_date']:%Y-%m-%d}",
            "entry_price": round(p["entry_price"], 2), "exit_date": f"{date:%Y-%m-%d}",
            "exit_price": round(price, 2), "shares": p["shares"], "reason": reason,
            "pnl_ils": round(pnl, 0), "pnl_pct": round((price / p["entry_price"] - 1) * 100, 2),
            "r_multiple": round((price - p["entry_price"]) / r, 2) if r > 0 else ""}


def main():
    t = today()
    mode = "paper" if PAPER_MODE else "real"
    usdils, fx_ok = usd_ils_rate()
    warnings = [] if fx_ok else ["לא התקבל שער דולר, הונח 3.7"]

    market, idx_dfs = {}, {}
    rmap = reddit_map()
    for m, sym in MARKET_INDEX.items():
        idx = get(sym)
        idx_dfs[m] = idx
        market[m] = bool(market_ok_series(idx).iloc[-1]) if idx is not None else False
        if idx is None:
            warnings.append(f"אין נתוני מדד {sym}, קניות חדשות בשוק הזה חסומות")

    lines = [f"📊 סריקה יומית {t:%d/%m/%Y}" + ("  |  🧪 מצב נייר" if PAPER_MODE else ""),
             f"שוק ת\"א: {'✅ מגמה חיובית' if market['TA'] else '⛔ אין קניות חדשות'}",
             f"שוק ארה\"ב: {'✅ מגמה חיובית' if market['US'] else '⛔ אין קניות חדשות'}", ""]

    # ---------- מצב נייר: ביצוע פקודות ממתינות במחיר הפתיחה ----------
    trades, pending, fills = [], {"entries": [], "exits": []}, []
    if PAPER_MODE:
        positions = [parse_pos(r) for r in read_csv(P_POS)]
        trades = read_csv(P_TRADES)
        if os.path.exists(PENDING):
            pending = json.load(open(PENDING, encoding="utf-8"))
        keep = []
        for ex in pending["exits"]:
            p = next((x for x in positions if x["symbol"] == ex["symbol"]), None)
            if p is None:
                continue
            d0, px = next_open(ex["symbol"], ex["signal_date"])
            if d0 is None:
                keep.append(ex)
                continue
            tr = close_trade(p, d0, px, ex["reason"], usdils)
            trades.append(tr)
            positions.remove(p)
            fills.append(f"✔️ נמכר {p['symbol']} ב-{fmt(p['symbol'], px)} | {tr['pnl_ils']:+,.0f} ש\"ח")
            log_event(mode, "SELL", p["symbol"], f"{px:.2f} x{p['shares']} pnl={tr['pnl_ils']}")
        pending["exits"] = keep
        keep = []
        for en in pending["entries"]:
            if any(x["symbol"] == en["symbol"] for x in positions):
                continue
            d0, px = next_open(en["symbol"], en["signal_date"])
            if d0 is None:
                keep.append(en)
                continue
            if len(positions) >= MAX_OPEN_POSITIONS:
                log_event(mode, "SKIP", en["symbol"], "התיק מלא")
                continue
            if en.get("limit") and px > en["limit"]:
                fills.append(f"⏭️ דילוג על {en['symbol']}: נפתח גבוה מדי, לא רודפים")
                log_event(mode, "SKIP", en["symbol"], f"פתיחה {px:.2f} מעל לימיט {en['limit']:.2f}")
                continue
            if px <= en["stop"]:
                fills.append(f"⏭️ דילוג על {en['symbol']}: נפתח מתחת לסטופ")
                log_event(mode, "SKIP", en["symbol"], f"פתיחה {px:.2f} מתחת לסטופ")
                continue
            positions.append({"symbol": en["symbol"], "entry_date": d0, "entry_price": px,
                              "shares": en["shares"], "initial_stop": en["stop"], "type": en["type"]})
            fills.append(f"✔️ נקנה {en['symbol']} ב-{fmt(en['symbol'], px)} x{en['shares']}")
            log_event(mode, "BUY", en["symbol"], f"{px:.2f} x{en['shares']} stop={en['stop']:.2f}")
        pending["entries"] = keep
        if fills:
            lines += ["ביצועים מהבוקר (נייר):"] + fills + [""]
    else:
        positions = [parse_pos(r) for r in read_csv("positions.csv")]

    realized = sum(float(x["pnl_ils"]) for x in trades)
    equity = CAPITAL_ILS + realized

    # ---------- בדיקת פוזיציות פתוחות ----------
    exits, holds, unrealized = [], [], 0.0
    pending_exit_syms = {e["symbol"] for e in pending["exits"]}
    for p in positions:
        d = get(p["symbol"])
        if d is None:
            warnings.append(f"אין נתונים עבור {p['symbol']}")
            continue
        d = add_indicators(d)
        start = d.index.searchsorted(p["entry_date"])
        if start >= len(d):
            continue
        reason, stop = exit_check(d, start, len(d) - 1, p["entry_price"], p["initial_stop"])
        close = d["Close"].iloc[-1]
        pnl = (close / p["entry_price"] - 1) * 100
        unrealized += p["shares"] * (to_ils(p["symbol"], close, usdils) - to_ils(p["symbol"], p["entry_price"], usdils))
        if reason and p["symbol"] not in pending_exit_syms:
            why = "הסגירה ירדה מתחת לסטופ" if reason == "stop" else f"{TIME_STOP_DAYS} ימים בלי התקדמות"
            exits.append(f"🔴 למכור {p['symbol']} ({p['shares']} מניות)\n"
                         f"   סיבה: {why}\n   סגירה: {fmt(p['symbol'], close)} | {pnl:+.1f}%")
            log_event(mode, "EXIT_SIGNAL", p["symbol"], f"{reason} close={close:.2f}")
            if PAPER_MODE:
                pending["exits"].append({"symbol": p["symbol"], "signal_date": f"{d.index[-1]:%Y-%m-%d}",
                                         "reason": reason})
        elif not reason:
            h = f"🟢 {p['symbol']}: {pnl:+.1f}% | סטופ: {fmt(p['symbol'], stop)}"
            de = days_to_next_earnings(earnings_dates(p["symbol"]), t)
            if de is not None and de <= EARNINGS_WARN_DAYS:
                h += f"\n   📅 דוח בעוד {de} ימים: סיכון לקפיצה מעל הסטופ"
            rn = reddit_note(p["symbol"], rmap)
            if rn:
                h += "\n   " + rn
            holds.append(h)
    if exits:
        lines += ["יציאות (לבצע בפתיחה):"] + exits + [""]
    if holds:
        lines += ["החזקות:"] + holds + [""]

    # ---------- כניסות חדשות ----------
    taken = {p["symbol"] for p in positions} | {e["symbol"] for e in pending["entries"]}
    free = MAX_OPEN_POSITIONS - len(positions) + len(exits) - len(pending["entries"])
    sector_count = {}
    for s_ in [p["symbol"] for p in positions] + [e["symbol"] for e in pending["entries"]]:
        sec = quality(s_)["sector"]
        sector_count[sec] = sector_count.get(sec, 0) + 1

    cands, blocked = [], []
    for sym in WATCHLIST_TA + WATCHLIST_US:
        if sym in taken:
            continue
        df = get(sym)
        if df is None:
            warnings.append(f"אין נתונים עבור {sym}")
            continue
        if (t - df.index[-1]).days > 1 or not market[market_of(sym)]:
            continue
        d = add_indicators(df)
        i = len(d) - 1
        edates = earnings_dates(sym)
        sig = entry_signal(d, i, reaction_days(edates, d.index))
        if not sig:
            continue
        de = days_to_next_earnings(edates, t)
        if sig != "post_earnings" and de is not None and de <= EARNINGS_BLACKOUT_DAYS:
            blocked.append(f"{sym} (דוח בעוד {de} ימים)")
            log_event(mode, "BLOCKED", sym, f"{sig} earnings in {de}d")
            continue
        q = quality(sym)
        if FUNDAMENTAL_FILTER and not q["ok"]:
            blocked.append(f"{sym} (איכות: {', '.join(q['failed'])})")
            log_event(mode, "BLOCKED", sym, f"{sig} quality {q['failed']}")
            continue
        cands.append({"sym": sym, "sig": sig, "d": d, "q": q, "de": de,
                      "rs": relative_strength(d, idx_dfs[market_of(sym)])})

    cands.sort(key=lambda c: c["rs"], reverse=True)       # החזקות ביותר קודם
    entries = []
    for rank, c in enumerate(cands, 1):
        sym, sig, d, q = c["sym"], c["sig"], c["d"], c["q"]
        i = len(d) - 1
        close, stop = d["Close"].iloc[i], initial_stop(d, i)
        limit = close + LIMIT_ATR * d["atr"].iloc[i]
        shares = position_size(to_ils(sym, close, usdils), to_ils(sym, stop, usdils), equity)
        value = shares * to_ils(sym, close, usdils)
        small = value < MIN_POSITION_ILS
        sector_full = sector_count.get(q["sector"], 0) >= MAX_PER_SECTOR
        txt = (f"🟡 #{rank} לקנות {sym}: {NAMES[sig]}\n"
               f"   סגירה: {fmt(sym, close)} | לא לקנות מעל: {fmt(sym, limit)}\n"
               f"   סטופ: {fmt(sym, stop)} (-{(close - stop) / close * 100:.1f}%)\n"
               f"   כמות: {shares} מניות (כ-{value:,.0f} ש\"ח)\n"
               f"   חוזק יחסי: {c['rs']:+.1f}% מול המדד | {quality_text(q)}\n"
               f"   סקטור: {q['sector']} | דוח הבא: "
               + (f"בעוד {c['de']} ימים" if c["de"] is not None else "לא ידוע"
                  + (" (בדוק במאיה)" if market_of(sym) == "TA" else "")))
        rn = reddit_note(sym, rmap)
        if rn:
            txt += "\n   " + rn
        if small:
            txt += "\n   ⚠️ פוזיציה קטנה מדי ביחס לעמלה, עדיף לדלג"
        if sector_full:
            txt += f"\n   ⚠️ כבר יש {MAX_PER_SECTOR} פוזיציות בסקטור הזה, עדיף לדלג"
        log_event(mode, "ENTRY_SIGNAL", sym,
                  f"{sig} close={close:.2f} stop={stop:.2f} shares={shares} rs={c['rs']:.1f}")
        if PAPER_MODE and not small and not sector_full and shares > 0 and free > 0:
            pending["entries"].append({"symbol": sym, "signal_date": f"{d.index[-1]:%Y-%m-%d}",
                                       "type": sig, "stop": round(float(stop), 4),
                                       "limit": round(float(limit), 4), "shares": shares})
            sector_count[q["sector"]] = sector_count.get(q["sector"], 0) + 1
            free -= 1
            txt += "\n   📝 נרשמה קנייה על הנייר לפתיחה הבאה"
        entries.append(txt)
    if entries:
        lines += [f"כניסות אפשריות (מדורגות לפי חוזק, מקומות פנויים: {max(free, 0)}):"] + entries
    else:
        lines.append("אין כניסות חדשות היום.")
    if blocked:
        lines += ["", "🚫 נחסמו היום: " + ", ".join(blocked)]

    # ---------- שמירה וסיכום ----------
    if PAPER_MODE:
        write_csv(P_POS, POS_F, [pos_row(p) for p in positions])
        write_csv(P_TRADES, TRADE_F, trades)
        json.dump(pending, open(PENDING, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        wins = sum(1 for x in trades if float(x["pnl_ils"]) > 0)
        rate = f"{wins / len(trades):.0%}" if trades else "אין עדיין"
        lines += ["", "📒 תיק הנייר:",
                  f"שווי משוער: {equity + unrealized:,.0f} ש\"ח (התחלה {CAPITAL_ILS:,.0f})",
                  f"רווח ממומש: {realized:+,.0f} | לא ממומש: {unrealized:+,.0f}",
                  f"עסקאות סגורות: {len(trades)} | הצלחה: {rate}"]
    else:
        lines += ["", "תזכורת: אחרי ביצוע, עדכן את positions.csv."]
    if warnings:
        lines += ["", "⚠️ הערות:"] + [f"• {w}" for w in warnings]
    send("\n".join(lines))


if __name__ == "__main__":
    main()
