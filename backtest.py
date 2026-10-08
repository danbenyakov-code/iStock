"""בדיקה היסטורית: איך האסטרטגיה הייתה מתפקדת ב-10 השנים האחרונות."""
import numpy as np
import pandas as pd
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


# ================= סימולציית תיק מלאה =================
def prepare(sym, df, mkt_ok, edates, idx_df):
    """מחשב מראש את כל אותות הכניסה של מניה, עם כל המסננים."""
    d = add_indicators(df)
    rdays = reaction_days(edates, d.index)
    idxc = idx_df["Close"].reindex(d.index, method="ffill") if idx_df is not None else None
    sig = {}
    for i in range(1, len(d)):
        t = entry_signal(d, i, rdays)
        day = d.index[i]
        if not t or not mkt_ok.get(day, False):
            continue
        if t != "post_earnings" and earnings_soon(edates, day, EARNINGS_BLACKOUT_DAYS):
            continue
        rs = 0.0
        if i > RS_LOOKBACK:
            rs = d["Close"].iloc[i] / d["Close"].iloc[i - RS_LOOKBACK] - 1
            if idxc is not None and pd.notna(idxc.iloc[i - RS_LOOKBACK]):
                rs -= idxc.iloc[i] / idxc.iloc[i - RS_LOOKBACK] - 1
        sig[day] = (t, initial_stop(d, i), d["Close"].iloc[i] + LIMIT_ATR * d["atr"].iloc[i], rs)
    return d, sig


def portfolio(data, usdils):
    """מריץ את הבוט כמו במציאות: הון אמיתי, מזומן, סיכון כולל, דירוג, עמלות."""
    ils = lambda sym, p: to_ils(sym, p, usdils)
    dates = sorted(set().union(*[set(d.index[200:]) for d, _ in data.values()]))
    where = {s: {day: i for i, day in enumerate(d.index)} for s, (d, _) in data.items()}
    cash, pos, pend_in, pend_out, last = float(CAPITAL_ILS), {}, {}, set(), {}
    curve, expo = {}, []
    for day in dates:
        for s in list(pend_out):                      # מכירות בפתיחה
            i = where[s].get(day)
            if i is None:
                continue
            p = pos.pop(s)
            cash += p["sh"] * ils(s, data[s][0]["Open"].iloc[i])
            cash -= COMMISSION_PCT_ROUNDTRIP * p["sh"] * ils(s, p["entry"])
            pend_out.discard(s)
        eq = cash + sum(p["sh"] * last.get(s, ils(s, p["entry"])) for s, p in pos.items())
        risk = sum(max(0.0, p["sh"] * (ils(s, p["entry"]) - ils(s, p.get("cur", p["stop"]))))
                   for s, p in pos.items())
        for s, (stop, limit) in list(pend_in.items()):  # קניות בפתיחה
            i = where[s].get(day)
            if i is None:
                continue
            del pend_in[s]
            px = data[s][0]["Open"].iloc[i]
            if s in pos or px > limit or px <= stop:
                continue
            per = ils(s, px) - ils(s, stop)
            sh = min(position_size(ils(s, px), ils(s, stop), eq), int(cash / ils(s, px)))
            if MAX_TOTAL_RISK:
                sh = min(sh, int(max(MAX_TOTAL_RISK * eq - risk, 0) / per))
            if sh <= 0 or sh * ils(s, px) < MIN_POSITION_ILS:
                continue
            cash -= sh * ils(s, px)
            pos[s] = {"i": i, "entry": px, "stop": stop, "sh": sh}
            risk += sh * per
        for s, p in pos.items():                      # בדיקת יציאה בסגירה
            d = data[s][0]
            i = where[s].get(day)
            if i is None:
                continue
            last[s] = ils(s, d["Close"].iloc[i])
            reason, p["cur"] = exit_check(d, p["i"], i, p["entry"], p["stop"])
            if s not in pend_out and reason:
                pend_out.add(s)
        cands = [(sig[day][3], s) for s, (_, sig) in data.items()
                 if day in sig and s not in pos and s not in pend_in]
        for _, s in sorted(cands, reverse=True):
            pend_in[s] = data[s][1][day][1:3]
        inv = sum(p["sh"] * last.get(s, ils(s, p["entry"])) for s, p in pos.items())
        curve[day] = cash + inv
        expo.append(inv / (cash + inv))
    return pd.Series(curve), float(np.mean(expo))


def metrics(c):
    c = c.dropna()
    yrs = (c.index[-1] - c.index[0]).days / 365.25
    cagr = (c.iloc[-1] / c.iloc[0]) ** (1 / yrs) - 1
    peak = c.cummax()
    return cagr, (c / peak - 1).min()


def yearly(c):
    y = c.groupby(c.index.year).last()
    prev = y.shift(1)
    prev.iloc[0] = c.iloc[0]
    return y / prev - 1


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
    mkt, idx_dfs = {}, {}
    for m, sym in MARKET_INDEX.items():
        idx = load(sym, period="10y")
        idx_dfs[m] = idx
        mkt[m] = market_ok_series(idx).to_dict() if idx is not None else {}
    all_tr, years, data = [], 0, {}
    for sym in WATCHLIST_TA + WATCHLIST_US:
        df = load(sym, period="10y")
        if df is None:
            continue
        years = max(years, (df.index[-1] - df.index[0]).days / 365)
        ed = earnings_dates(sym)
        all_tr += simulate(sym, add_indicators(df), mkt[market_of(sym)], ed)
        data[sym] = prepare(sym, df, mkt[market_of(sym)], ed, idx_dfs[market_of(sym)])

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
    cmp_text = compare(data)
    send("\n".join(lines))
    send(cmp_text)
    import os, datetime as _dt
    os.makedirs("logs", exist_ok=True)
    with open("logs/backtest.md", "w", encoding="utf-8") as f:
        f.write(f"# בדיקה היסטורית | {_dt.date.today():%d/%m/%Y}\n\n```\n" + "\n".join(lines)
                + "\n```\n\n```\n" + cmp_text + "\n```\n")


def compare(data):
    usdils, _ = usd_ils_rate()
    curve, expo = portfolio(data, usdils)
    start = curve.index[0]
    bench = {}
    for name, sym in (("ת\"א 35", MARKET_INDEX["TA"]), ("S&P 500", "SPY")):
        b = load(sym, period="10y")
        if b is not None:
            bench[name] = b["Close"][b.index >= start]

    cg, dd = metrics(curve)
    peak = curve.cummax()
    lines = ["⚖️ הבוט מול קנייה והחזקה", "",
             f"🤖 הבוט (תיק {CAPITAL_ILS:,.0f} ש\"ח, ללא הגבלת פוזיציות):",
             f"   {cg:+.1%} בשנה | ירידה מקסימלית {dd:.1%} ({(curve - peak).min():,.0f} ש\"ח)",
             f"   כסף מושקע בממוצע: {expo:.0%} מהתיק (השאר במזומן)", ""]
    ratio = cg / abs(dd) if dd else 0
    beat = []
    for name, c in bench.items():
        bc, bd = metrics(c)
        br = bc / abs(bd) if bd else 0
        lines.append(f"📈 {name}: {bc:+.1%} בשנה | ירידה מקסימלית {bd:.1%} | יחס {br:.2f}")
        beat.append(cg >= bc)
    lines += ["", f"יחס תשואה לסיכון של הבוט (תשואה ÷ ירידה מקסימלית): {ratio:.2f}"]
    if bench:
        if all(beat):
            verdict = "✅ הבוט ניצח את שני המדדים בתשואה."
        elif any(beat):
            verdict = "🟡 הבוט ניצח מדד אחד והפסיד לשני."
        else:
            verdict = "🔴 שני המדדים הניבו יותר מהבוט. בדוק אם הבוט לפחות מסוכן פחות (יחס ירידה)."
        lines += ["", "שורה תחתונה: " + verdict, "", "תשואת הבוט לפי שנים:"]
        yb = yearly(curve)
        lines += [f"{y}: {yb[y]:+.1%}" for y in yb.index]
    lines += ["", "הערות: המדד הישראלי בלי דיבידנדים (בפועל טוב בכ-1%-2% בשנה).",
              "שער הדולר קבוע לאורך כל התקופה. סינון האיכות וסקטורים לא כלולים."]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
