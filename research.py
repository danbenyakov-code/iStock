"""מחברת מחקר: ניתוח יומי של כל מניה ברשימה, גם אלה שלא נקנו."""
from config import *
from strategy import market_of
from insights import quality, quality_text, reddit_note, days_to_next_earnings, earnings_dates, relative_strength

ORDER = ["💼 בתיק", "🎯 איתות היום", "👀 קרובה לאיתות", "⏳ מגמה חיובית, ממתינה", "⛔ שוק חסום", "❌ לא מעניינת"]


def fmt(sym, p):
    return f"{p:,.1f} אג'" if market_of(sym) == "TA" else f"${p:,.2f}"


def analyze(sym, d, t, market_ok, held, signaled, idx_df, rmap, light=False):
    row = d.iloc[-1]
    close, sma200, sma50 = row["Close"], row["sma200"], row["sma50"]
    rsi, high20 = row["rsi"], row["high20"]
    volx = row["Volume"] / row["vol20"] if row["vol20"] else 0
    dist = close / high20 - 1
    above = close / sma200 - 1
    uptrend = close > sma200 and sma50 > sma200
    rs = relative_strength(d, idx_df)
    if light:                                     # סיווג מהיר, בלי קריאות רשת
        if sym in held:
            cat = ORDER[0]
        elif sym in signaled:
            cat = ORDER[1]
        elif not market_ok:
            cat = ORDER[4]
        elif close < sma200:
            cat = ORDER[5]
        elif (uptrend and rsi < 40) or dist > -0.03:
            cat = ORDER[2]
        elif uptrend:
            cat = ORDER[3]
        else:
            cat = ORDER[5]
        return {"sym": sym, "cat": cat, "rs": rs}
    q = quality(sym)
    de = days_to_next_earnings(earnings_dates(sym), t)

    if sym in held:
        cat, verdict = ORDER[0], "מוחזקת. הניהול לפי הסטופ בדוח היומי"
    elif sym in signaled:
        cat, verdict = ORDER[1], "נתנה איתות היום. ראה פירוט בפקודות או בחסימות"
    elif not market_ok:
        cat, verdict = ORDER[4], "המדד מתחת לממוצע 200 יום. אין קניות חדשות בשוק הזה"
    elif close < sma200:
        cat, verdict = ORDER[5], f"מתחת לממוצע 200 יום ({above:+.0%}). מגמה שלילית, לא נוגעים"
    elif uptrend and rsi < 40:
        cat = ORDER[2]
        verdict = f"תיקון בעיצומו בתוך מגמת עלייה (RSI {rsi:.0f}). אם RSI יחזור מעל 40, תהיה קנייה בתיקון"
    elif dist >= 0:
        cat = ORDER[2]
        verdict = (f"כבר {dist:.1%} מעל שיא 20 יום ({fmt(sym, high20)}), אבל המחזור חלש (פי {volx:.1f}). "
                   f"סגירה מעל השיא עם מחזור פי 1.5 ומעלה תהיה פריצה")
    elif dist > -0.03:
        cat = ORDER[2]
        verdict = (f"{abs(dist):.1%} מתחת לשיא 20 יום ({fmt(sym, high20)}). "
                   f"סגירה מעליו עם מחזור פי 1.5 ומעלה תהיה פריצה")
    elif uptrend:
        cat, verdict = ORDER[3], "מגמת עלייה תקינה, אבל לא בתיקון ולא ליד פריצה. מחכים לנקודת כניסה"
    else:
        cat, verdict = ORDER[5], "מעל ממוצע 200, אבל ממוצע 50 מתחתיו. מגמה לא מבוססת"

    if cat in ORDER[2:4] and FUNDAMENTAL_FILTER and not q["ok"]:
        verdict += f". שים לב: תיחסם בסינון האיכות ({', '.join(q['failed'])})"
    if cat in ORDER[2:4] and de is not None and de <= EARNINGS_BLACKOUT_DAYS:
        verdict += f". שים לב: דוח בעוד {de} ימים, כניסה תיחסם עד אחריו"

    text = (f"**{sym}** {fmt(sym, close)} | {above:+.0%} מול ממוצע 200 | RSI {rsi:.0f} | "
            f"{dist:+.1%} משיא 20 יום | מחזור פי {volx:.1f} | חוזק יחסי {rs:+.1f}% | "
            f"{quality_text(q)} | דוח: {f'בעוד {de} ימים' if de is not None else 'לא ידוע'}")
    rn = reddit_note(sym, rmap)
    if rn:
        text += " | " + rn.replace("\n", " ")
    short = ("תיקון, RSI %.0f" % rsi) if (uptrend and rsi < 40) else \
        (f"מעל השיא, מחזור חלש" if dist >= 0 else f"{abs(dist):.1%} מתחת לשיא")
    return {"sym": sym, "cat": cat, "text": text + f"\n  → {verdict}", "short": short}


def build(t, ind, market, idx_dfs, held, signaled, rmap, symbols=None):
    symbols = symbols or (WATCHLIST_TA + WATCHLIST_US)
    light = []
    for sym in symbols:
        d = ind(sym)
        if d is None or len(d) < 2 or pd_isna(d["sma200"].iloc[-1]):
            continue
        m = market_of(sym)
        light.append((analyze(sym, d, t, market[m], held, signaled, idx_dfs[m], rmap, light=True), d))
    wide = len(symbols) > 40
    near = sorted([x for x in light if x[0]["cat"] == ORDER[2]], key=lambda x: x[0]["rs"], reverse=True)
    detail = [x for x in light if x[0]["cat"] in ORDER[:2]] + near[:RESEARCH_MAX]
    if not wide:
        detail = light
    notes = []
    for x, d in detail:
        m = market_of(x["sym"])
        notes.append(analyze(x["sym"], d, t, market[m], held, signaled, idx_dfs[m], rmap))
    md = [f"# מחברת מחקר | {t:%d/%m/%Y}", "",
          f"נסרקו {len(light)} מניות." + (f" מפורטות: בתיק, איתותים, ו-{min(len(near), RESEARCH_MAX)} "
                                         f"הקרובות ביותר לאיתות (לפי חוזק יחסי)." if wide else ""), ""]
    for cat in ORDER:
        group = [n for n in notes if n["cat"] == cat]
        count = sum(1 for x, _ in light if x["cat"] == cat)
        if group:
            extra = f" (מתוך {count})" if count > len(group) else ""
            md += [f"## {cat} ({len(group)}){extra}", ""] + [f"- {n['text']}" for n in group] + [""]
        elif count:
            md += [f"## {cat}: {count} מניות", ""]
    watch = [f"{n['sym']} ({n['short']})" for n in notes if n["cat"] == ORDER[2]][:8]
    return notes, "\n".join(md), watch


def pd_isna(x):
    return x != x
