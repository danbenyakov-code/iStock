"""בדיקה היסטורית: איך האסטרטגיה הייתה מתפקדת ב-10 השנים האחרונות."""
import numpy as np
from config import *
from strategy import *
from bot import send
from insights import earnings_dates, reaction_days, earnings_soon


def simulate(symbol, d, mkt_ok, edates):
    trades, i, n = [], 1, len(d)
    rdays = reaction_days(edates, d.index)
    while i < n - 2:
        sig = entry_signal(d, i, rdays)
        if sig and sig != "post_earnings" and earnings_soon(edates, d.index[i], EARNINGS_BLACKOUT_DAYS):
            sig = None                                # חסימה לפני דוח
        if sig and mkt_ok.get(d.index[i], False):
            stop = initial_stop(d, i)
            limit = d["Close"].iloc[i] + LIMIT_ATR * d["atr"].iloc[i]
            entry_i = i + 1
            entry = d["Open"].iloc[entry_i]          # כניסה בפתיחה למחרת
            r = entry - stop
            if r <= 0 or entry > limit:
                i += 1
                continue
            j = entry_i
            while j < n - 1:
                reason, _ = exit_check(d, entry_i, j, entry, stop)
                if reason:
                    break
                j += 1
            exit_price = d["Open"].iloc[j + 1] if j < n - 1 else d["Close"].iloc[-1]
            cost_r = COMMISSION_PCT_ROUNDTRIP * entry / r
            trades.append({"symbol": symbol, "type": sig, "market": market_of(symbol),
                           "r": (exit_price - entry) / r - cost_r, "days": j + 1 - entry_i})
            i = j + 1
        else:
            i += 1
    return trades


def stats(tr, label):
    if not tr:
        return f"{label}: אין עסקאות"
    r = np.array([t["r"] for t in tr])
    wins, losses = r[r > 0], r[r <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() else float("inf")
    streak = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    return (f"{label}: {len(r)} עסקאות | הצלחה {len(wins) / len(r):.0%} | "
            f"ממוצע {r.mean():+.2f}R | פקטור רווח {pf:.2f} | רצף הפסדים {streak}")


def main():
    mkt = {}
    for m, sym in MARKET_INDEX.items():
        idx = load(sym, period="10y")
        mkt[m] = market_ok_series(idx).to_dict() if idx is not None else {}
    all_tr, years = [], 0
    for sym in WATCHLIST_TA + WATCHLIST_US:
        df = load(sym, period="10y")
        if df is None:
            continue
        years = max(years, (df.index[-1] - df.index[0]).days / 365)
        all_tr += simulate(sym, add_indicators(df), mkt[market_of(sym)], earnings_dates(sym))

    total_r = sum(t["r"] for t in all_tr)
    est = total_r * RISK_PER_TRADE * 100 / years if years else 0
    lines = ["🧪 בדיקה היסטורית (אחרי עמלות)", "",
             stats(all_tr, "סה\"כ"),
             stats([t for t in all_tr if t["type"] == "pullback"], "תיקון"),
             stats([t for t in all_tr if t["type"] == "breakout"], "פריצה"),
             stats([t for t in all_tr if t["type"] == "post_earnings"], "אחרי דוח"),
             stats([t for t in all_tr if t["market"] == "TA"], "ת\"א"),
             stats([t for t in all_tr if t["market"] == "US"], "ארה\"ב"), "",
             f"סה\"כ {total_r:+.1f}R על פני {years:.1f} שנים",
             f"הערכה גסה: כ-{est:+.1f}% בשנה לפני מס (בלי מגבלת 5 פוזיציות)", "",
             "R = הסכום שסיכנת בעסקה. +0.3R בממוצע = רווח של 30% מהסיכון לכל עסקה.",
             "לא נבדק היסטורית: סינון האיכות (אין נתוני עבר אמינים) ודירוג החוזק.",
             "ביצועי עבר אינם מבטיחים ביצועים עתידיים."]
    send("\n".join(lines))


if __name__ == "__main__":
    main()
