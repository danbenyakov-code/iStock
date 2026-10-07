"""מנהל תיק: סורק, מחליט, מבצע על הנייר במחירי שוק אמיתיים, ומדווח כמו ברוקר."""
import csv
import datetime as dt
import json
import os
import requests
import pandas as pd
from config import *
from strategy import *
from insights import *
import research
import ai
import universe
import manual

NAMES = {"post_earnings": "תגובה חזקה לדוח", "pullback": "קנייה בתיקון", "breakout": "פריצה"}
LOG = "logs"
F = {k: f"{LOG}/{v}" for k, v in {
    "pos": "paper_positions.csv", "trades": "paper_trades.csv", "events": "events.csv",
    "pending": "paper_pending.json", "state": "portfolio.json",
    "equity": "daily_equity.csv", "changelog": "CHANGELOG.md"}.items()}
POS_F = ["symbol", "entry_date", "entry_price", "entry_fx", "shares", "initial_stop", "type", "reason"]
TRADE_F = ["symbol", "type", "entry_date", "entry_price", "exit_date", "exit_price", "shares",
           "cost_ils", "proceeds_ils", "pnl_ils", "pnl_pct", "r_multiple", "days",
           "entry_reason", "exit_reason"]
EQ_F = ["date", "cash", "invested", "equity", "day_pct", "total_pct", "drawdown_pct", "positions"]
EVENT_F = ["date", "mode", "event", "symbol", "details"]
_cache, _ind = {}, {}
UNIVERSE = list(WATCHLIST_TA) + list(WATCHLIST_US)


def liquid(sym, d, fx):
    """מסנן נזילות: מחזור יומי ממוצע ומחיר מינימלי."""
    tail = d.iloc[-20:]
    turnover = float((tail["Close"] * tail["Volume"]).mean())
    if market_of(sym) == "TA":
        return turnover / 100 >= MIN_TURNOVER_ILS_TA
    return turnover * fx >= MIN_TURNOVER_ILS_US and float(d["Close"].iloc[-1]) >= MIN_PRICE_US


# ===================== כלים =====================
def today():
    return pd.Timestamp(dt.datetime.now(dt.timezone.utc).date())


def get(sym):
    if sym not in _cache:
        _cache[sym] = load(sym)
    return _cache[sym]


def ind(sym):
    if sym not in _ind:
        df = get(sym)
        _ind[sym] = add_indicators(df) if df is not None else None
    return _ind[sym]


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]
    return [r for r in rows if not str(next(iter(r.values()), "")).startswith("#")]


def write_csv(path, fields, rows):
    os.makedirs(LOG, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_event(mode, event, symbol, details):
    os.makedirs(LOG, exist_ok=True)
    new = not os.path.exists(F["events"])
    with open(F["events"], "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_F)
        if new:
            w.writeheader()
        w.writerow({"date": f"{today():%Y-%m-%d}", "mode": mode, "event": event,
                    "symbol": symbol, "details": details})


def send(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(text)
        return
    print(text)                                   # תמיד גם ביומן של GitHub
    for k in range(0, len(text), 3500):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text[k:k + 3500]}, timeout=30)
        if r.status_code != 200:                  # כישלון שליחה = שגיאה אדומה, לא שקט
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:300]}")


def fmt(sym, p):
    return f"{p:,.1f} אג'" if market_of(sym) == "TA" else f"${p:,.2f}"


def ils(sym, price, fx):
    return price / 100 if market_of(sym) == "TA" else price * fx


def money(x):
    return f"{x:,.0f} ₪"


def parse_pos(r, fx):
    return {"symbol": r["symbol"].strip().upper(), "entry_date": pd.Timestamp(r["entry_date"].strip()),
            "entry_price": float(r["entry_price"]),
            "entry_fx": float(r["entry_fx"]) if r.get("entry_fx") else fx,
            "shares": int(float(r["shares"])), "initial_stop": float(r["initial_stop"]),
            "type": r.get("type") or "", "reason": r.get("reason") or ""}


def next_open(sym, after_date):
    df = get(sym)
    if df is None:
        return None, None
    later = df.index[df.index > pd.Timestamp(after_date)]
    return (later[0], float(df.loc[later[0], "Open"])) if len(later) else (None, None)


# ===================== ניתוח =====================
def evaluate(p, fx):
    """מצב פוזיציה בסגירה האחרונה: מחיר, סטופ נוכחי, ואם צריך לצאת."""
    d = ind(p["symbol"])
    if d is None:
        return None
    start = d.index.searchsorted(p["entry_date"])
    i = len(d) - 1
    if start > i:
        start = i
    reason, stop = exit_check(d, start, i, p["entry_price"], p["initial_stop"])
    close = float(d["Close"].iloc[i])
    cost = p["shares"] * ils(p["symbol"], p["entry_price"], p["entry_fx"])
    value = p["shares"] * ils(p["symbol"], close, fx)
    why = None
    if reason == "stop":
        kind = "סטופ נגרר (נעילת רווח)" if stop > p["initial_stop"] + 1e-9 else "סטופ התחלתי (קטיעת הפסד)"
        why = f"{kind}: סגירה {fmt(p['symbol'], close)} מתחת ל-{fmt(p['symbol'], stop)}"
    elif reason == "time":
        why = (f"יציאה בזמן: {i - start} ימי מסחר בלי התקדמות של 1R "
               f"({close / p['entry_price'] - 1:+.1%})")
    return {"close": close, "stop": float(stop), "reason": reason, "why": why,
            "cost": cost, "value": value, "pnl": value - cost,
            "pct": (close / p["entry_price"] - 1) * 100,
            "risk": max(0.0, p["shares"] * (ils(p["symbol"], p["entry_price"], p["entry_fx"])
                                            - ils(p["symbol"], stop, fx)))}


def entry_reason(sig, d, i, sym, rs, rank, q, de):
    row, prev = d.iloc[i], d.iloc[i - 1]
    if sig == "pullback":
        why = (f"מגמת עלייה ({row['Close'] / row['sma200'] - 1:+.0%} מעל ממוצע 200 יום), "
               f"RSI עלה מ-{prev['rsi']:.0f} ל-{row['rsi']:.0f}: התיקון מסתיים")
    elif sig == "breakout":
        why = (f"פריצה מעל שיא 20 יום ({fmt(sym, row['high20'])}), "
               f"מחזור פי {row['Volume'] / row['vol20']:.1f} מהממוצע")
    else:
        why = (f"תגובה לדוח: פתיחה בפער {row['Open'] / prev['Close'] - 1:+.1%}, "
               f"מחזור פי {row['Volume'] / row['vol20']:.1f}, סגירה חזקה")
    nxt = f"בעוד {de} ימים" if de is not None else "לא ידוע"
    return (f"{why}. חוזק יחסי {rs:+.1f}% מול המדד (מקום {rank}). "
            f"{quality_text(q)}. סקטור: {q['sector']}. דוח הבא: {nxt}")


def find_candidates(t, taken, market, idx_dfs, mode, fx=3.7):
    cands, blocked = [], []
    for sym in UNIVERSE:
        if sym in taken:
            continue
        d = ind(sym)
        if d is None:
            continue
        if (t - d.index[-1]).days > 1 or not market[market_of(sym)]:
            continue
        i = len(d) - 1
        if not liquid(sym, d, fx):
            continue
        sig = entry_signal(d, i, None)
        row, prev = d.iloc[i], d.iloc[i - 1]
        gap = row["Open"] > prev["Close"] * 1.04 and row["vol20"] > 0 and row["Volume"] > 2 * row["vol20"]
        edates = None
        if gap:                                   # אולי יום דוח: בודקים רק אז (חוסך זמן)
            edates = earnings_dates(sym)
            sig = entry_signal(d, i, reaction_days(edates, d.index)) or sig
        if not sig:
            continue
        if edates is None:
            edates = earnings_dates(sym)
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
        cands.append({"sym": sym, "sig": sig, "d": d, "i": i, "q": q, "de": de,
                      "close": float(d["Close"].iloc[i]), "stop": float(initial_stop(d, i)),
                      "limit": float(d["Close"].iloc[i] + LIMIT_ATR * d["atr"].iloc[i]),
                      "rs": relative_strength(d, idx_dfs[market_of(sym)])})
    cands.sort(key=lambda c: c["rs"], reverse=True)
    for rank, c in enumerate(cands, 1):
        c["rank"] = rank
        c["reason"] = entry_reason(c["sig"], c["d"], c["i"], c["sym"], c["rs"], rank, c["q"], c["de"])
    return cands, blocked


def size_order(sym, price, stop, fx, equity, cash, open_risk):
    """כמה מניות לקנות: לפי סיכון, תקרת פוזיציה, מזומן וסיכון כולל."""
    px, st = ils(sym, price, fx), ils(sym, stop, fx)
    per = px - st
    if per <= 0:
        return 0, "סטופ לא תקין"
    sh = position_size(px, st, equity)
    sh = min(sh, int(cash / px))
    if MAX_TOTAL_RISK:
        room = MAX_TOTAL_RISK * equity - open_risk
        sh = min(sh, int(max(room, 0) / per))
    if sh <= 0 or sh * px < MIN_POSITION_ILS:
        if cash / px * px < MIN_POSITION_ILS:
            return 0, "אין מספיק מזומן"
        return 0, "תקרת הסיכון הכולל של התיק מלאה"
    return sh, None


# ===================== מצב נייר =====================
def load_state(fx, idx_dfs, positions, trades):
    if os.path.exists(F["state"]):
        return json.load(open(F["state"], encoding="utf-8"))
    realized = sum(float(x.get("pnl_ils") or 0) for x in trades)
    cost = sum(p["shares"] * ils(p["symbol"], p["entry_price"], p["entry_fx"]) for p in positions)
    bench = {}
    for name, sym in (("TA", MARKET_INDEX["TA"]), ("US", "SPY")):
        b = get(sym)
        if b is not None:
            bench[name] = float(b["Close"].iloc[-1])
    return {"start_capital": CAPITAL_ILS, "start_date": f"{today():%Y-%m-%d}",
            "cash": CAPITAL_ILS + realized - cost, "peak": CAPITAL_ILS, "bench_start": bench}


def run_paper(t, fx, market, idx_dfs, rmap, warnings):
    mode = "paper"
    trades = read_csv(F["trades"])
    positions = [parse_pos(r, fx) for r in read_csv(F["pos"])]
    state = load_state(fx, idx_dfs, positions, trades)
    pending = json.load(open(F["pending"], encoding="utf-8")) if os.path.exists(F["pending"]) \
        else {"entries": [], "exits": []}
    done, cl_done = [], []

    # ---- 1. מכירות בפתיחה ----
    keep = []
    for ex in pending.get("exits", []):
        p = next((x for x in positions if x["symbol"] == ex["symbol"]), None)
        if p is None:
            continue
        d0, px = next_open(p["symbol"], ex["signal_date"])
        if d0 is None:
            keep.append(ex)
            continue
        sym = p["symbol"]
        cost = p["shares"] * ils(sym, p["entry_price"], p["entry_fx"])
        proceeds = p["shares"] * ils(sym, px, fx)
        comm = COMMISSION_PCT_ROUNDTRIP * cost
        pnl = proceeds - cost - comm
        state["cash"] += proceeds - comm
        r = p["entry_price"] - p["initial_stop"]
        df = get(sym)
        days = int(((df.index > p["entry_date"]) & (df.index <= d0)).sum()) if df is not None else 0
        tr = {"symbol": sym, "type": p["type"], "entry_date": f"{p['entry_date']:%Y-%m-%d}",
              "entry_price": round(p["entry_price"], 2), "exit_date": f"{d0:%Y-%m-%d}",
              "exit_price": round(px, 2), "shares": p["shares"], "cost_ils": round(cost),
              "proceeds_ils": round(proceeds), "pnl_ils": round(pnl),
              "pnl_pct": round(pnl / cost * 100, 2), "r_multiple": round((px - p["entry_price"]) / r, 2) if r > 0 else "",
              "days": days, "entry_reason": p["reason"], "exit_reason": ex.get("why", ex.get("reason", ""))}
        trades.append(tr)
        positions.remove(p)
        icon = "✅" if pnl > 0 else "❌"
        line = (f"🔴 מכירה {sym}: {p['shares']} × {fmt(sym, px)} = {money(proceeds)}\n"
                f"   {icon} {pnl:+,.0f} ₪ ({tr['pnl_pct']:+.1f}%, {tr['r_multiple']}R) אחרי {days} ימים\n"
                f"   למה: {tr['exit_reason']}")
        done.append(line)
        cl_done.append(f"- 🔴 **מכירה {sym}**: {p['shares']} × {fmt(sym, px)} = {money(proceeds)} | "
                       f"{pnl:+,.0f} ₪ ({tr['pnl_pct']:+.1f}%) | {days} ימים | {tr['exit_reason']}")
        log_event(mode, "SELL", sym, f"{px:.2f} x{p['shares']} pnl={round(pnl)}")
    pending["exits"] = keep

    # ---- 2. קניות בפתיחה ----
    evals = {p["symbol"]: evaluate(p, fx) for p in positions}
    invested = sum(e["value"] for e in evals.values() if e)
    open_risk = sum(e["risk"] for e in evals.values() if e)
    keep = []
    for en in pending.get("entries", []):
        sym = en["symbol"]
        if any(x["symbol"] == sym for x in positions):
            continue
        d0, px = next_open(sym, en["signal_date"])
        if d0 is None:
            keep.append(en)
            continue
        skip = None
        if px > en["limit"]:
            skip = f"נפתח ב-{fmt(sym, px)}, מעל מחיר המקסימום {fmt(sym, en['limit'])}. לא רודפים"
        elif px <= en["stop"]:
            skip = "נפתח מתחת לסטופ"
        else:
            equity = state["cash"] + invested
            sh, err = size_order(sym, px, en["stop"], fx, equity, state["cash"], open_risk)
            if err:
                skip = err
        if skip:
            done.append(f"⏭️ לא נקנה {sym}: {skip}")
            cl_done.append(f"- ⏭️ לא נקנה {sym}: {skip}")
            log_event(mode, "SKIP", sym, skip)
            continue
        cost = sh * ils(sym, px, fx)
        state["cash"] -= cost
        p = {"symbol": sym, "entry_date": d0, "entry_price": px, "entry_fx": fx, "shares": sh,
             "initial_stop": en["stop"], "type": en["type"], "reason": en["reason"]}
        positions.append(p)
        invested += cost
        open_risk += sh * (ils(sym, px, fx) - ils(sym, en["stop"], fx))
        done.append(f"🟢 קנייה {sym}: {sh} × {fmt(sym, px)} = {money(cost)}\n"
                    f"   אסטרטגיה: {NAMES[en['type']]} | סטופ: {fmt(sym, en['stop'])}\n"
                    f"   למה: {en['reason']}")
        cl_done.append(f"- 🟢 **קנייה {sym}**: {sh} × {fmt(sym, px)} = {money(cost)} | "
                       f"{NAMES[en['type']]} | סטופ {fmt(sym, en['stop'])} | {en['reason']}")
        log_event(mode, "BUY", sym, f"{px:.2f} x{sh} stop={en['stop']:.2f}")
    pending["entries"] = keep

    if state.get("done_date") == f"{t:%Y-%m-%d}":    # הרצה חוזרת: לא לאבד את מה שבוצע
        done = state.get("done_tg", []) + done
        cl_done = state.get("done_cl", []) + cl_done
    state.update({"done_date": f"{t:%Y-%m-%d}", "done_tg": done, "done_cl": cl_done})

    # ---- 3. מצב בסגירה ----
    evals = {p["symbol"]: evaluate(p, fx) for p in positions}
    holds, exits_tomorrow, cl_exits = [], [], []
    for p in positions:
        e = evals[p["symbol"]]
        sym = p["symbol"]
        if e is None:
            warnings.append(f"אין נתונים עבור {sym}")
            continue
        h = (f"{'🟢' if e['pnl'] >= 0 else '🔻'} {sym} | {p['shares']} × {fmt(sym, p['entry_price'])} → "
             f"{fmt(sym, e['close'])}\n   {e['pct']:+.1f}% ({e['pnl']:+,.0f} ₪) | שווי {money(e['value'])} | "
             f"סטופ {fmt(sym, e['stop'])}")
        de = days_to_next_earnings(earnings_dates(sym), t)
        if de is not None and de <= EARNINGS_WARN_DAYS:
            h += f"\n   📅 דוח בעוד {de} ימים"
        rn = reddit_note(sym, rmap)
        if rn:
            h += "\n   " + rn
        holds.append(h)
        if e["reason"] and not any(x["symbol"] == sym for x in pending["exits"]):
            pending["exits"].append({"symbol": sym, "signal_date": f"{ind(sym).index[-1]:%Y-%m-%d}",
                                     "reason": e["reason"], "why": e["why"]})
            exits_tomorrow.append(f"🔴 מכירה {sym} ({p['shares']} מניות)\n   למה: {e['why']}")
            cl_exits.append(f"- 🔴 מכירה מתוכננת {sym}: {e['why']}")
            log_event(mode, "EXIT_SIGNAL", sym, e["why"])

    invested = sum(e["value"] for e in evals.values() if e)
    equity = state["cash"] + invested

    # ---- 4. הזדמנויות חדשות ותכנון תקציב ----
    exiting = {x["symbol"] for x in pending["exits"]}
    budget = state["cash"] + sum(evals[s]["value"] for s in exiting if evals.get(s))
    open_risk = sum(e["risk"] for s, e in evals.items() if e and s not in exiting)
    sectors = {}
    for p in positions:
        if p["symbol"] not in exiting:
            sec = quality(p["symbol"])["sector"]
            sectors[sec] = sectors.get(sec, 0) + 1
    waiting = []                                       # פקודות שעוד מחכות ליום מסחר
    for e in pending["entries"]:
        d = ind(e["symbol"])
        if d is not None and e["signal_date"] != f"{d.index[-1]:%Y-%m-%d}":
            waiting.append(e)
    pending["entries"] = waiting
    taken = {p["symbol"] for p in positions} | {e["symbol"] for e in waiting}
    cands, blocked = find_candidates(t, taken, market, idx_dfs, mode, fx)
    orders, cl_orders = [], []
    for c in cands:
        sym = c["sym"]
        sh, err = size_order(sym, c["close"], c["stop"], fx, equity, budget, open_risk)
        sec = c["q"]["sector"]
        if not err and MAX_PER_SECTOR and sectors.get(sec, 0) >= MAX_PER_SECTOR:
            err = f"כבר יש {MAX_PER_SECTOR} פוזיציות בסקטור {sec}"
        log_event(mode, "ENTRY_SIGNAL", sym, f"{c['sig']} close={c['close']:.2f} rs={c['rs']:.1f} {err or 'planned'}")
        if err:
            orders.append(f"⏭️ #{c['rank']} {sym}: איתות {NAMES[c['sig']]}, לא נכנס ({err})")
            cl_orders.append(f"- ⏭️ {sym}: איתות {NAMES[c['sig']]}, לא נכנס ({err})")
            continue
        cost = sh * ils(sym, c["close"], fx)
        budget -= cost
        open_risk += sh * (ils(sym, c["close"], fx) - ils(sym, c["stop"], fx))
        sectors[sec] = sectors.get(sec, 0) + 1
        pending["entries"].append({"symbol": sym, "signal_date": f"{c['d'].index[-1]:%Y-%m-%d}",
                                   "type": c["sig"], "stop": round(c["stop"], 4),
                                   "limit": round(c["limit"], 4), "reason": c["reason"]})
        txt = (f"🟡 #{c['rank']} קנייה {sym}: כ-{sh} מניות (כ-{money(cost)})\n"
               f"   אסטרטגיה: {NAMES[c['sig']]} | לא מעל {fmt(sym, c['limit'])} | סטופ {fmt(sym, c['stop'])}\n"
               f"   למה: {c['reason']}")
        rn = reddit_note(sym, rmap)
        if rn:
            txt += "\n   " + rn
        orders.append(txt)
        cl_orders.append(f"- 🟡 קנייה מתוכננת {sym} (כ-{sh} מניות): {NAMES[c['sig']]} | {c['reason']}")

    # ---- 5. תמונת מצב ושמירה ----
    hist = [r for r in read_csv(F["equity"]) if r.get("date") != f"{t:%Y-%m-%d}"]
    prev_eq = float(hist[-1]["equity"]) if hist else state["start_capital"]
    state["peak"] = max(state["peak"], equity)
    dd = equity / state["peak"] - 1
    total = equity / state["start_capital"] - 1
    day = equity / prev_eq - 1
    hist.append({"date": f"{t:%Y-%m-%d}", "cash": round(state["cash"]), "invested": round(invested),
                 "equity": round(equity), "day_pct": round(day * 100, 2), "total_pct": round(total * 100, 2),
                 "drawdown_pct": round(dd * 100, 2), "positions": len(positions)})
    max_dd = min(float(r["drawdown_pct"]) for r in hist)

    bench_txt = []
    for name, key, sym in (("ת\"א 35", "TA", MARKET_INDEX["TA"]), ("S&P 500", "US", "SPY")):
        b = get(sym)
        if b is not None and key in state.get("bench_start", {}):
            bench_txt.append(f"{name} {float(b['Close'].iloc[-1]) / state['bench_start'][key] - 1:+.1%}")

    wins = [float(x["pnl_ils"]) for x in trades if float(x["pnl_ils"]) > 0]
    losses = [float(x["pnl_ils"]) for x in trades if float(x["pnl_ils"]) <= 0]
    realized = sum(wins) + sum(losses)

    L = [f"📊 דוח תיק יומי | {t:%d/%m/%Y} | 🧪 נייר",
         f"💼 שווי התיק: {money(equity)}",
         f"היום: {equity - prev_eq:+,.0f} ₪ ({day:+.2%}) | מההתחלה: {equity - state['start_capital']:+,.0f} ₪ ({total:+.2%})",
         f"מזומן: {money(state['cash'])} ({state['cash'] / equity:.0%}) | מושקע: {invested / equity:.0%} | פוזיציות: {len(positions)}",
         f"ירידה מהשיא: {dd:.1%} | המקסימלית: {max_dd:.1f}%"]
    if bench_txt:
        L.append(f"מדדים מאז {state['start_date']}: " + " | ".join(bench_txt))
    L.append(f"שוק: ת\"א {'✅' if market['TA'] else '⛔'} | ארה\"ב {'✅' if market['US'] else '⛔'}")
    if done:
        L += ["", "🔄 בוצע בפתיחה:"] + done
    if holds:
        L += ["", f"📂 החזקות ({len(holds)}):"] + holds
    skipped = [o for o in orders if o.startswith("⏭️")]
    if len(skipped) > 5:                          # בטלגרם מקצרים, ביומן הכול נשמר
        orders = [o for o in orders if not o.startswith("⏭️")] + skipped[:5] + \
                 [f"⏭️ ועוד {len(skipped) - 5} איתותים שלא נכנסו (פירוט ב-CHANGELOG)"]
    if exits_tomorrow or orders:
        L += ["", "📝 פקודות לפתיחה הבאה:"] + exits_tomorrow + orders
    else:
        L += ["", "📝 אין פקודות חדשות."]
    if blocked:
        L += ["", "🚫 נחסמו: " + ", ".join(blocked)]
    if trades:
        L += ["", f"📈 עסקאות סגורות: {len(trades)} | הצלחה {len(wins) / len(trades):.0%} | "
                  f"רווח ממומש {realized:+,.0f} ₪",
              f"רווח ממוצע {sum(wins) / len(wins) if wins else 0:+,.0f} ₪ | "
              f"הפסד ממוצע {sum(losses) / len(losses) if losses else 0:+,.0f} ₪"]
    signaled = {c["sym"] for c in cands} | {b.split(" ")[0] for b in blocked}
    notes, research_md, watch = research.build(t, ind, market, idx_dfs,
                                               {p["symbol"] for p in positions}, signaled, rmap, UNIVERSE)
    if watch:
        L += ["", "👀 קרובות לאיתות: " + ", ".join(watch)]
    ai_text, ai_err = ai.commentary("\n".join(L), research_md)
    if ai_err:
        warnings.append(ai_err)
    if warnings:
        L += ["", "⚠️ " + " | ".join(warnings)]

    # ---- יומן CHANGELOG ----
    block = [f"## {t:%d/%m/%Y}",
             f"**שווי: {money(equity)}** | היום {day:+.2%} | מההתחלה {total:+.2%} | "
             f"מזומן {money(state['cash'])} | פוזיציות {len(positions)} | ירידה מהשיא {dd:.1%}", ""]
    block += (cl_done or ["- לא בוצעו פעולות"]) + cl_exits + cl_orders
    block.append(f"- 📓 מחקר מלא על כל המניות{' ופרשנות אנליסט' if ai_text else ''}: "
                 f"[research/{t:%Y-%m-%d}.md](research/{t:%Y-%m-%d}.md)")
    if positions:
        block += ["", "| מניה | כמות | כניסה | נוכחי | % | ₪ | סטופ |", "|---|---|---|---|---|---|---|"]
        for p in positions:
            e = evals[p["symbol"]]
            if e:
                block.append(f"| {p['symbol']} | {p['shares']} | {fmt(p['symbol'], p['entry_price'])} | "
                             f"{fmt(p['symbol'], e['close'])} | {e['pct']:+.1f}% | {e['pnl']:+,.0f} | "
                             f"{fmt(p['symbol'], e['stop'])} |")
    old = ""
    if os.path.exists(F["changelog"]):
        txt = open(F["changelog"], encoding="utf-8").read()
        old = txt.split("\n", 2)[2] if txt.startswith("# ") else txt
    if old.startswith(f"## {t:%d/%m/%Y}"):          # הרצה חוזרת באותו יום: מחליפים את הבלוק
        nxt = old.find("\n## ")
        old = old[nxt + 1:] if nxt != -1 else ""

    os.makedirs(LOG, exist_ok=True)
    with open(F["changelog"], "w", encoding="utf-8") as f:
        f.write("# יומן תיק הנייר\n\n" + "\n".join(block) + "\n\n" + old.lstrip())
    write_csv(F["pos"], POS_F, [{**p, "entry_date": f"{p['entry_date']:%Y-%m-%d}"} for p in positions])
    write_csv(F["trades"], TRADE_F, trades)
    write_csv(F["equity"], EQ_F, hist)
    json.dump(pending, open(F["pending"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(state, open(F["state"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return finish(t, L, research_md, ai_text)


def finish(t, L, research_md, ai_text):
    """שומר את מחברת המחקר ומחזיר את ההודעות לשליחה."""
    os.makedirs(f"{LOG}/research", exist_ok=True)
    body = research_md
    if ai_text:
        body += "\n\n## 🧠 פרשנות אנליסט (AI)\n\n" + ai_text + \
                "\n\n_פרשנות בלבד. ההחלטות מתקבלות לפי כללי המערכת._"
    with open(f"{LOG}/research/{t:%Y-%m-%d}.md", "w", encoding="utf-8") as f:
        f.write(body)
    msgs = ["\n".join(L)]
    if ai_text:
        msgs.append("🧠 פרשנות אנליסט (AI)\n\n" + ai_text +
                    "\n\nפרשנות בלבד. ההחלטות מתקבלות לפי כללי המערכת.")
    return msgs


# ===================== מצב כסף אמיתי =====================
def run_real(t, fx, market, idx_dfs, rmap, warnings):
    mode = "real"
    positions = [parse_pos(r, fx) for r in read_csv("positions.csv")]
    L = [f"📊 סריקה יומית {t:%d/%m/%Y} | 💰 כסף אמיתי",
         f"שוק: ת\"א {'✅' if market['TA'] else '⛔'} | ארה\"ב {'✅' if market['US'] else '⛔'}", ""]
    open_risk, invested = 0.0, 0.0
    for p in positions:
        e = evaluate(p, fx)
        if not e:
            continue
        invested += e["value"]
        if e["reason"]:
            L.append(f"🔴 למכור {p['symbol']} ({p['shares']}): {e['why']}")
            log_event(mode, "EXIT_SIGNAL", p["symbol"], e["why"])
        else:
            open_risk += e["risk"]
            L.append(f"🟢 {p['symbol']}: {e['pct']:+.1f}% | עדכן סטופ ל-{fmt(p['symbol'], e['stop'])}")
    cash = max(CAPITAL_ILS - invested, 0)
    cands, blocked = find_candidates(t, {p["symbol"] for p in positions}, market, idx_dfs, mode, fx)
    for c in cands:
        sh, err = size_order(c["sym"], c["close"], c["stop"], fx, CAPITAL_ILS, cash, open_risk)
        log_event(mode, "ENTRY_SIGNAL", c["sym"], f"{c['sig']} {err or 'ok'}")
        if err:
            L.append(f"⏭️ #{c['rank']} {c['sym']}: {err}")
            continue
        cash -= sh * ils(c["sym"], c["close"], fx)
        L.append(f"🟡 #{c['rank']} לקנות {c['sym']}: {sh} מניות | לא מעל {fmt(c['sym'], c['limit'])} | "
                 f"סטופ {fmt(c['sym'], c['stop'])}\n   למה: {c['reason']}")
    if blocked:
        L.append("🚫 נחסמו: " + ", ".join(blocked))
    signaled = {c["sym"] for c in cands} | {b.split(" ")[0] for b in blocked}
    _, research_md, watch = research.build(t, ind, market, idx_dfs,
                                           {p["symbol"] for p in positions}, signaled, rmap, UNIVERSE)
    if watch:
        L += ["", "👀 קרובות לאיתות: " + ", ".join(watch)]
    L += ["", "תזכורת: אחרי ביצוע, עדכן את positions.csv."]
    ai_text, ai_err = ai.commentary("\n".join(L), research_md)
    if ai_err:
        warnings.append(ai_err)
    if warnings:
        L += ["⚠️ " + " | ".join(warnings)]
    return finish(t, L, research_md, ai_text)


def main():
    global UNIVERSE
    t = today()
    ta, us = universe.get()
    held = [r["symbol"] for r in read_csv(F["pos"])] + [r["symbol"] for r in read_csv("positions.csv")]
    UNIVERSE = sorted(set(ta) | set(us) | set(held))
    if len(UNIVERSE) > 40:                        # הורדה מרוכזת במקום מניה אחרי מניה
        _cache.update(universe.bulk_load([s for s in UNIVERSE if s not in _cache]))
    fx, fx_ok = usd_ils_rate()
    warnings = [] if fx_ok else ["לא התקבל שער דולר, הונח 3.7"]
    if UNIVERSE_MODE == "wide" and len(us) < 300:
        warnings.append(f"רשימת S&P 500 לא נמשכה, נסרקות {len(us)} מניות ארה\"ב מרשימת גיבוי")
    market, idx_dfs = {}, {}
    for m, sym in MARKET_INDEX.items():
        idx = get(sym)
        idx_dfs[m] = idx
        market[m] = bool(market_ok_series(idx).iloc[-1]) if idx is not None else False
        if idx is None:
            warnings.append(f"אין נתוני מדד {sym}")
    rmap = reddit_map()
    run = run_paper if PAPER_MODE else run_real
    msgs = run(t, fx, market, idx_dfs, rmap, warnings)
    loaded = sum(1 for s in UNIVERSE if _cache.get(s) is not None)
    msgs[0] += f"\n\n🔎 נסרקו {loaded} מניות"
    try:
        mine = manual.summary_for_daily_report()
        if mine:
            msgs.append(mine)
    except Exception as e:
        print("manual summary failed:", e)
    for msg in msgs:
        send(msg)


if __name__ == "__main__":
    main()
