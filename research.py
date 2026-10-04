"""מחברת מחקר: ניתוח יומי של כל מניה ברשימה, גם אלה שלא נקנו."""
from config import *
from strategy import market_of
from insights import quality, quality_text, reddit_note, days_to_next_earnings, earnings_dates, relative_strength

ORDER = ["💼 בתיק", "🎯 איתות היום", "👀 קרובה לאיתות", "⏳ מגמה חיובית, ממתינה", "⛔ שוק חסום", "❌ לא מעניינת"]


def fmt(sym, p):
    return f"{p:,.1f} אג'" if market_of(sym) == "TA" else f"${p:,.2f}"


def analyze(sym, d, t, market_ok, held, signaled, idx_df, rmap):
    row = d.iloc[-1]
    close, sma200, sma50 = row["Close"], row["sma200"], row["sma50"]
    rsi, high20 = row["rsi"], row["high20"]
    volx = row["Volume"] / row["vol20"] if row["vol20"] else 0
    dist = close / high20 - 1
    above = close / sma200 - 1
    uptrend = close > sma200 and sma50 > sma200
    rs = relative_strength(d, idx_df)
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


def build(t, ind, market, idx_dfs, held, signaled, rmap):
    notes = []
    for sym in WATCHLIST_TA + WATCHLIST_US:
        d = ind(sym)
        if d is None or len(d) < 2:
            continue
        m = market_of(sym)
        notes.append(analyze(sym, d, t, market[m], held, signaled, idx_dfs[m], rmap))
    md = [f"# מחברת מחקר | {t:%d/%m/%Y}", ""]
    for cat in ORDER:
        group = [n for n in notes if n["cat"] == cat]
        if group:
            md += [f"## {cat} ({len(group)})", ""] + [f"- {n['text']}" for n in group] + [""]
    watch = [f"{n['sym']} ({n['short']})" for n in notes if n["cat"] == ORDER[2]]
    return notes, "\n".join(md), watch
