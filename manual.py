"""התיק שלי: תיק דמו ידני שמנוהל בפקודות טלגרם.

פקודות (בעברית או באנגלית):
  קנה TEVA.TA 100        קנייה של 100 מניות במחיר השוק
  קנה AAPL 5000          קנייה בסכום של כ-5,000 ש"ח
  מכור TEVA.TA           מכירת כל הפוזיציה
  מכור TEVA.TA 40        מכירה חלקית
  ביטול                  ביטול פקודות שממתינות לפתיחת המסחר
  מצב                    שווי, רווח ותשואה של שני התיקים
  עזרה                   רשימת הפקודות
"""
import csv
import datetime as dt
import json
import os
import re
import requests
import pandas as pd
import yfinance as yf
from config import CAPITAL_ILS, COMMISSION_PCT_ROUNDTRIP

DIR = "logs/manual"
STATE, POS, TRADES, ORDERS, CHANGELOG = (f"{DIR}/{x}" for x in
    ("state.json", "positions.csv", "trades.csv", "orders.csv", "CHANGELOG.md"))
POS_F = ["symbol", "shares", "avg_price", "currency", "cost_ils", "opened"]
TRADE_F = ["time", "side", "symbol", "shares", "price", "currency", "fx", "value_ils",
           "commission_ils", "pnl_ils", "pnl_pct"]
ORDER_F = ["received", "command", "status", "details"]
SIDE_FEE = COMMISSION_PCT_ROUNDTRIP / 2
FRESH_MINUTES = 45            # בר אחרון לפני פחות מזה = השוק פתוח (כולל עיכוב של Yahoo)
_px, _fx = {}, {}


# ===================== כלים =====================
def now():
    return dt.datetime.now(dt.timezone.utc)


def il_time(t=None):
    t = t or now()
    return t.astimezone(dt.timezone(dt.timedelta(hours=3))).strftime("%d/%m %H:%M")


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def write_csv(path, fields, rows):
    os.makedirs(DIR, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_csv(path, fields, row):
    new = not os.path.exists(path)
    os.makedirs(DIR, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE, encoding="utf-8"))
    return {"cash": float(CAPITAL_ILS), "start_capital": float(CAPITAL_ILS),
            "start_date": now().strftime("%Y-%m-%d"), "offset": 0, "pending": [], "peak": float(CAPITAL_ILS)}


def save_state(s):
    os.makedirs(DIR, exist_ok=True)
    json.dump(s, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def money(x):
    return f"{x:,.0f} ₪"


def tg(method, **data):
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return {}
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:300]}")
    return r.json()


def reply(text):
    print(text)
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if chat and os.environ.get("TELEGRAM_TOKEN"):
        for k in range(0, len(text), 3500):
            tg("sendMessage", chat_id=chat, text=text[k:k + 3500])


# ===================== מחירים =====================
def quote(sym):
    """(מחיר, מטבע, האם השוק פתוח, זמן הבר). None אם הסימבול לא קיים."""
    if sym in _px:
        return _px[sym]
    out = None
    try:
        t = yf.Ticker(sym)
        h = t.history(period="5d", interval="5m")
        if h is not None and not h.empty:
            last = h.index[-1]
            last = last.tz_convert("UTC") if last.tzinfo else last.tz_localize("UTC")
            age = (pd.Timestamp(now()) - last).total_seconds() / 60
            cur = (t.fast_info.get("currency") or "USD") if hasattr(t, "fast_info") else "USD"
            out = (float(h["Close"].iloc[-1]), cur, age <= FRESH_MINUTES, last)
    except Exception:
        out = None
    _px[sym] = out
    return out


def to_ils_rate(cur):
    """כמה ש"ח שווה יחידת מחיר אחת במטבע הזה."""
    cur = cur or "USD"
    if cur in _fx:
        return _fx[cur]
    sub = {"ILA": ("ILS", 0.01), "GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01)}
    base, mult = sub.get(cur, (cur, 1.0))
    rate = 1.0
    if base != "ILS":
        try:
            h = yf.Ticker(f"{base}ILS=X").history(period="5d")
            rate = float(h["Close"].iloc[-1])
        except Exception:
            rate = 3.7 if base == "USD" else None
    _fx[cur] = rate * mult if rate else None
    return _fx[cur]


def fmt(price, cur):
    if cur == "ILA":
        return f"{price:,.1f} אג'"
    sign = {"USD": "$", "EUR": "€", "GBP": "£"}.get(cur)
    return f"{sign}{price:,.2f}" if sign else f"{price:,.2f} {cur}"


# ===================== פקודות =====================
HELP = ("📘 פקודות לתיק שלי (אפשר גם דרך התפריט / בטלגרם):\n"
        "/buy TEVA.TA 100: קנייה של 100 מניות במחיר השוק\n"
        "/buy AAPL 5000ש\"ח: קנייה בסכום של כ-5,000 ₪\n"
        "/sell TEVA.TA: מכירת כל הפוזיציה\n"
        "/sell TEVA.TA 40: מכירה חלקית\n"
        "/cancel: ביטול פקודות שממתינות לפתיחה\n"
        "/status: שווי, רווח ותשואה של שני התיקים\n"
        "/analyze NVDA: ניתוח מלא של מניה (טכני, פונדמנטלי, ודעת הבוט)\n"
        "אפשר גם בעברית: קנה, מכור, ביטול, מצב.\n"
        "רשימה: כתוב 'קנייה' ובשורות הבאות מניה בכל שורה. בלי סכום = חלוקה שווה של המזומן.\n"
        "אפשר שמות חברות (Nvidia, Tesla, אפל) או סימבולים.\n\n"
        "מניות בת\"א עם הסיומת .TA (למשל LUMI.TA). בלי סיומת = ארה\"ב.\n"
        "הבוט בודק פקודות כל 30 דקות בערך, והמחירים מתעכבים בכ-15 דקות.\n"
        "פקודה מחוץ לשעות המסחר תבוצע כשהשוק ייפתח.")


COMMANDS = [("buy", "קנייה: /buy TEVA.TA 100 או /buy AAPL 5000ש\"ח"),
            ("sell", "מכירה: /sell TEVA.TA או /sell TEVA.TA 40"),
            ("status", "מצב התיק שלי ותיק הבוט"),
            ("analyze", "ניתוח מניה: /analyze NVDA או /analyze TEVA.TA"),
            ("cancel", "ביטול פקודות ממתינות"),
            ("help", "רשימת הפקודות")]


def set_commands(state):
    """רושם את תפריט ה-/ בטלגרם (פעם אחת)."""
    if state.get("commands_v") == 2 or not os.environ.get("TELEGRAM_TOKEN"):
        return
    tg("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in COMMANDS],
                                            ensure_ascii=False))
    state["commands_v"] = 2


# שמות חברות נפוצים -> סימבול (כולל עברית). מוצג תמיד בתשובה, כך שרואים מה הובן.
ALIASES = {
    "nvidia": "NVDA", "אנבידיה": "NVDA", "tesla": "TSLA", "טסלה": "TSLA", "apple": "AAPL", "אפל": "AAPL",
    "microsoft": "MSFT", "מיקרוסופט": "MSFT", "amazon": "AMZN", "אמזון": "AMZN", "google": "GOOGL",
    "alphabet": "GOOGL", "גוגל": "GOOGL", "meta": "META", "facebook": "META", "netflix": "NFLX",
    "palantir": "PLTR", "פלנטיר": "PLTR", "sofi": "SOFI", "rocketlab": "RKLB", "rocket lab": "RKLB",
    "rocket": "RKLB", "broadcom": "AVGO", "marvell": "MRVL", "credo": "CRDO", "arista": "ANET",
    "applied materials": "AMAT", "palo alto": "PANW", "pan": "PANW", "intel": "INTC", "coinbase": "COIN",
    "micron": "MU", "oracle": "ORCL", "salesforce": "CRM", "uber": "UBER", "zim": "ZIM", "זים": "ZIM",
    "s&p": "SPY", "s&p500": "SPY", "sp500": "SPY", "nasdaq": "QQQ", "נאסדק": "QQQ",
    "לאומי": "LUMI.TA", "פועלים": "POLI.TA", "דיסקונט": "DSCT.TA", "מזרחי": "MZTF.TA",
    "בזק": "BEZQ.TA", "אלעל": "ELAL.TA", "אל על": "ELAL.TA",
}
# מניות שנסחרות גם בת"א וגם בארה"ב: שואלים למה התכוונת
DUAL = {"TEVA": "טבע", "טבע": "טבע", "NICE": "נייס", "נייס": "נייס", "ESLT": "אלביט", "אלביט": "אלביט",
        "ELBIT": "אלביט", "TSEM": "טאואר", "טאואר": "טאואר", "TOWER": "טאואר", "NVMI": "נובה", "נובה": "נובה",
        "ICL": "כיל", "כיל": "כיל", "CAMT": "קמטק", "קמטק": "קמטק"}
DUAL_SYM = {"טבע": "TEVA", "נייס": "NICE", "אלביט": "ESLT", "טאואר": "TSEM", "נובה": "NVMI", "כיל": "ICL",
            "קמטק": "CAMT"}


def resolve(token):
    """שם או סימבול -> (סימבול, None) או (None, שאלת הבהרה)."""
    raw = token.strip().strip(".,;:")
    key = raw.lower()
    if key in ALIASES:
        return ALIASES[key], None
    up = raw.upper()
    dual = DUAL.get(up) or DUAL.get(raw)
    if dual and not up.endswith(".TA"):
        b = DUAL_SYM[dual]
        return None, (f"❓ {raw}: {dual} נסחרת גם בת\"א וגם בארה\"ב. למה התכוונת?\n"
                      f"/buy {b}.TA (ת\"א, בשקלים) או /buy {b} (ארה\"ב, בדולרים)")
    if re.fullmatch(r"[A-Z0-9.\-^=]{1,12}", up):
        return up, None
    return None, f"❓ לא זיהיתי את \"{raw}\". נסה עם הסימבול, למשל NVDA או TEVA.TA."


BATCH_HEAD = r"^/?(קנייה|קניה|קנה|קני|buy|מכירה|מכור|sell)$"


def parse_batch(t):
    """רשימה: שורה ראשונה 'קנייה' ואחריה מניה בכל שורה, או 'קנה A B C' בשורה אחת."""
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    if not lines:
        return None
    head = lines[0].split()
    if not re.match(BATCH_HEAD, head[0], re.IGNORECASE):
        return None
    side = "sell" if head[0].lower().lstrip("/") in ("מכירה", "מכור", "sell") else "buy"
    items = []
    rest = [" ".join(head[1:])] if len(head) > 1 else []
    rest += lines[1:]
    for ln in rest:
        parts = ln.replace("₪", " ש\"ח").split()
        m = re.match(r"^(.+?)\s+(\d+(?:\.\d+)?)\s*(ש\"?ח|nis|ils)?$", " ".join(parts), re.IGNORECASE)
        if m:
            items.append((m.group(1), float(m.group(2)), bool(m.group(3))))
        elif len(lines) == 1:                         # 'קנה A B C' בשורה אחת
            items += [(p, None, False) for p in parts]
        else:
            items.append((" ".join(parts), None, False))
    if len(items) < 2:
        return None
    return {"cmd": "batch", "side": side, "items": items}


def parse(text):
    t0 = text.strip().replace(",", "")
    t0 = re.sub(r"^(/\w+)@\w+", r"\1", t0)
    b = parse_batch(t0)
    if b:
        return b
    t = text.strip().replace("₪", "").replace(",", "")
    t = re.sub(r"^(/\w+)@\w+", r"\1", t)          # /buy@MyBot -> /buy
    low = t.lower()
    if low in ("/buy", "/sell", "קנה", "מכור", "/analyze", "נתח", "ניתוח"):
        return {"cmd": "usage"}
    m = re.match(r"^/?(analyze|נתח|ניתוח)\s+(.+)$", t, re.IGNORECASE)
    if m:
        sym, ask = resolve(m.group(2))
        if ask:
            return {"cmd": "ask", "text": ask.replace("/buy", "/analyze")}
        return {"cmd": "analyze", "sym": sym}
    if low in ("מצב", "status", "/status", "תיק", "/start"):
        return {"cmd": "status"}
    if low in ("עזרה", "help", "/help"):
        return {"cmd": "help"}
    if low in ("ביטול", "cancel", "/cancel"):
        return {"cmd": "cancel"}
    m = re.match(r"^/?(קנה|קני|buy|מכור|sell)\s+([^\s\d][^\s]*)(?:\s+(\d+(?:\.\d+)?))?\s*(ש\"?ח|nis|ils)?$",
                 t, re.IGNORECASE)
    if not m:
        return {"cmd": "unknown"}
    side = "buy" if m.group(1).lower() in ("קנה", "קני", "buy") else "sell"
    sym, ask = resolve(m.group(2))
    if ask:
        return {"cmd": "ask", "text": ask if side == "buy" else ask.replace("/buy", "/sell")}
    num = float(m.group(3)) if m.group(3) else None
    if side == "buy" and num is None:
        return {"cmd": "usage"}
    # קנייה: מספר עד 10,000 = כמות מניות, אלא אם צוין ש"ח. מעל 10,000 = סכום (בטעות סביר יותר).
    by_amount = side == "buy" and (bool(m.group(4)) or (num is not None and num > 10_000))
    return {"cmd": side, "sym": sym, "qty": None if by_amount else num, "amount": num if by_amount else None}


def positions():
    return {r["symbol"]: {"shares": float(r["shares"]), "avg_price": float(r["avg_price"]),
                          "currency": r["currency"], "cost_ils": float(r["cost_ils"]), "opened": r["opened"]}
            for r in read_csv(POS)}


def save_positions(pos):
    write_csv(POS, POS_F, [{"symbol": s, **{k: (round(v, 4) if isinstance(v, float) else v)
                                           for k, v in p.items()}} for s, p in pos.items()])


def changelog(line):
    os.makedirs(DIR, exist_ok=True)
    old = open(CHANGELOG, encoding="utf-8").read().split("\n", 2)[-1] if os.path.exists(CHANGELOG) else ""
    with open(CHANGELOG, "w", encoding="utf-8") as f:
        f.write("# יומן התיק שלי\n\n" + f"- {il_time()} | {line}\n" + old.lstrip("\n"))


def execute(order, state, pos):
    """מבצע פקודה. מחזיר (טקסט, סטטוס): done, queued, rejected."""
    sym = order["sym"]
    q = quote(sym)
    if q is None:
        return f"❓ לא מצאתי את הסימבול {sym}. מניות בת\"א צריכות סיומת .TA (למשל TEVA.TA).", "rejected"
    price, cur, is_open, _ = q
    rate = to_ils_rate(cur)
    if rate is None:
        return f"❓ אין שער המרה למטבע {cur}, לא ניתן לסחור ב-{sym}.", "rejected"
    if not is_open:
        return f"⏳ השוק של {sym} סגור כרגע. הפקודה נשמרה ותבוצע במחיר השוק כשייפתח.", "queued"
    px_ils = price * rate

    if order["cmd"] == "buy":
        qty = int(order["qty"]) if order.get("qty") else \
            int(min(order["amount"], state["cash"]) / (px_ils * (1 + SIDE_FEE)))
        if qty <= 0:
            return f"❌ הסכום קטן ממחיר מניה אחת של {sym} ({money(px_ils)}).", "rejected"
        value = qty * px_ils
        fee = value * SIDE_FEE
        if value + fee > state["cash"]:
            maxq = int(state["cash"] / (px_ils * (1 + SIDE_FEE)))
            return (f"❌ אין מספיק מזומן: נדרש {money(value + fee)}, יש {money(state['cash'])}. "
                    f"אפשר לקנות עד {maxq} מניות."), "rejected"
        state["cash"] -= value + fee
        p = pos.get(sym)
        if p:
            tot = p["shares"] + qty
            p["avg_price"] = (p["avg_price"] * p["shares"] + price * qty) / tot
            p["shares"], p["cost_ils"] = tot, p["cost_ils"] + value + fee
        else:
            pos[sym] = {"shares": float(qty), "avg_price": price, "currency": cur,
                        "cost_ils": value + fee, "opened": now().strftime("%Y-%m-%d")}
        append_csv(TRADES, TRADE_F, {"time": il_time(), "side": "קנייה", "symbol": sym, "shares": qty,
                                      "price": round(price, 4), "currency": cur, "fx": round(rate, 4),
                                      "value_ils": round(value), "commission_ils": round(fee, 1)})
        txt = (f"🟢 בוצע: קנייה {sym}\n{qty} × {fmt(price, cur)} = {money(value)} (עמלה {money(fee)})\n"
               f"מזומן שנותר: {money(state['cash'])}")
        changelog(f"🟢 קנייה {sym}: {qty} × {fmt(price, cur)} = {money(value)}")
        return txt, "done"

    # מכירה
    p = pos.get(sym)
    if not p:
        return f"❌ אין לך פוזיציה ב-{sym}.", "rejected"
    qty = int(order["qty"]) if order.get("qty") else int(p["shares"])
    if qty <= 0 or qty > p["shares"]:
        return f"❌ יש לך רק {int(p['shares'])} מניות {sym}.", "rejected"
    value = qty * px_ils
    fee = value * SIDE_FEE
    cost_part = p["cost_ils"] * qty / p["shares"]
    pnl = value - fee - cost_part
    state["cash"] += value - fee
    p["shares"] -= qty
    p["cost_ils"] -= cost_part
    if p["shares"] <= 0:
        del pos[sym]
    append_csv(TRADES, TRADE_F, {"time": il_time(), "side": "מכירה", "symbol": sym, "shares": qty,
                                  "price": round(price, 4), "currency": cur, "fx": round(rate, 4),
                                  "value_ils": round(value), "commission_ils": round(fee, 1),
                                  "pnl_ils": round(pnl), "pnl_pct": round(pnl / cost_part * 100, 2)})
    icon = "✅" if pnl > 0 else "❌"
    txt = (f"🔴 בוצע: מכירה {sym}\n{qty} × {fmt(price, cur)} = {money(value)}\n"
           f"{icon} רווח/הפסד: {pnl:+,.0f} ₪ ({pnl / cost_part:+.1%})\nמזומן: {money(state['cash'])}")
    changelog(f"🔴 מכירה {sym}: {qty} × {fmt(price, cur)} | {pnl:+,.0f} ₪")
    return txt, "done"


def run_batch(o, text, rec, state, pos):
    """מבצע רשימת פקודות. קנייה בלי סכום = חלוקה שווה של המזומן."""
    good, notes = [], []
    for name, num, is_ils in o["items"]:
        sym, ask = resolve(name)
        if ask:
            notes.append(ask if o["side"] == "buy" else ask.replace("/buy", "/sell"))
        elif sym in [g[0] for g in good]:
            continue
        else:
            good.append((sym, num, is_ils))
    lines = [f"📋 התקבלה רשימה של {len(o['items'])} פריטים ({'קנייה' if o['side'] == 'buy' else 'מכירה'})"]
    if o["side"] == "buy":
        fixed = sum((n if (ils_ or n > 10_000) else 0) for _, n, ils_ in good if n)
        free = [g for g in good if g[1] is None]
        share = max(state["cash"] - fixed, 0) / len(free) if free else 0
        if free:
            lines.append(f"חלוקה שווה: כ-{money(share)} לכל אחת מ-{len(free)} מניות")
    done = queued = 0
    for sym, num, is_ils in good:
        if o["side"] == "buy":
            if num is None:
                order = {"cmd": "buy", "sym": sym, "qty": None, "amount": share}
            elif is_ils or num > 10_000:
                order = {"cmd": "buy", "sym": sym, "qty": None, "amount": num}
            else:
                order = {"cmd": "buy", "sym": sym, "qty": num, "amount": None}
        else:
            order = {"cmd": "sell", "sym": sym, "qty": num, "amount": None}
        order.update({"text": f"{'/buy' if o['side'] == 'buy' else '/sell'} {sym}"
                              + (f" {money(order['amount'])}" if order.get("amount") else
                                 f" {int(num)}" if num else ""), "received": rec})
        txt, st = execute(order, state, pos)
        if st == "queued":
            state["pending"].append(order)
            queued += 1
            lines.append(f"⏳ {sym}: ממתינה לפתיחת המסחר")
        else:
            append_csv(ORDERS, ORDER_F, {"received": rec, "command": order["text"], "status": st,
                                          "details": txt.replace("\n", " | ")})
            done += st == "done"
            lines.append(txt.split("\n")[0] + (" | " + txt.split("\n")[1] if st == "done" else ""))
    lines += notes
    lines.append(f"\nסיכום: {done} בוצעו, {queued} ממתינות לפתיחה, מזומן {money(state['cash'])}")
    if queued:
        lines.append("הממתינות יבוצעו בבדיקה הראשונה אחרי פתיחת השוק (ארה\"ב: 16:30 שעון ישראל).")
    return "\n".join(lines)


# ===================== ניתוח מניה =====================
def analyze_text(sym, state, pos):
    from strategy import load, add_indicators, entry_signal, initial_stop, market_ok_series
    from insights import (quality, earnings_dates, days_to_next_earnings, reaction_days,
                          relative_strength, reddit_map, reddit_note)
    import research
    from config import MARKET_INDEX, ATR_STOP_MULT, LIMIT_ATR, RISK_PER_TRADE, MAX_POSITION_PCT, \
        EARNINGS_BLACKOUT_DAYS

    df = load(sym, period="2y")
    if df is None:
        return f"❓ אין מספיק נתונים על {sym} (צריך שנה של מסחר לפחות). מניות בת\"א עם .TA."
    d = add_indicators(df)
    i, row = len(d) - 1, d.iloc[-1]
    c = float(row["Close"])
    q = quote(sym)
    cur = q[1] if q else ("ILA" if sym.endswith(".TA") else "USD")
    rate = to_ils_rate(cur) or 1.0
    mkt = "TA" if sym.endswith(".TA") else "US"
    idx = load(MARKET_INDEX[mkt], period="2y")
    mkt_ok = bool(market_ok_series(idx).iloc[-1]) if idx is not None else False

    def chg(n):
        return f"{c / float(d['Close'].iloc[-1 - n]) - 1:+.1%}" if len(d) > n else "?"
    hi, lo = float(d["High"].iloc[-252:].max()), float(d["Low"].iloc[-252:].min())

    try:
        import yfinance as _yf
        info = _yf.Ticker(sym).info or {}
    except Exception:
        info = {}
    qa = quality(sym)
    ed = earnings_dates(sym)
    de = days_to_next_earnings(ed, pd.Timestamp(now().date()))
    sig = entry_signal(d, i, reaction_days(ed, d.index))
    note = research.analyze(sym, d, pd.Timestamp(now().date()), mkt_ok, set(pos), {sym} if sig else set(),
                            idx, reddit_map())
    verdict = note["text"].split("→", 1)[-1].strip()

    L = [f"🔬 ניתוח {sym}" + (f" | {info.get('shortName')}" if info.get("shortName") else ""),
         f"מחיר: {fmt(c, cur)} | יום {chg(1)} | חודש {chg(21)} | 3 חודשים {chg(63)} | שנה {chg(252)}",
         f"טווח 52 שבועות: {fmt(lo, cur)} עד {fmt(hi, cur)} ({c / hi - 1:+.0%} מהשיא)", "",
         "📈 טכני:",
         f"• מגמה: {'עולה' if c > row['sma200'] and row['sma50'] > row['sma200'] else 'יורדת' if c < row['sma200'] else 'לא ברורה'}"
         f" | {c / row['sma50'] - 1:+.1%} מממוצע 50 | {c / row['sma200'] - 1:+.1%} מממוצע 200",
         f"• RSI: {row['rsi']:.0f}" + (" (קנוי יתר)" if row["rsi"] > 70 else " (מכור יתר)" if row["rsi"] < 30 else ""),
         f"• תנודתיות יומית (ATR): {row['atr'] / c:.1%} | מחזור היום פי {row['Volume'] / row['vol20']:.1f} מהממוצע"
         if row["vol20"] else f"• תנודתיות יומית (ATR): {row['atr'] / c:.1%}",
         f"• חוזק יחסי 3 חודשים מול המדד: {relative_strength(d, idx):+.1f}%",
         f"• מצב השוק ({MARKET_INDEX[mkt]}): {'✅ מעל ממוצע 200' if mkt_ok else '⛔ מתחת לממוצע 200'}", "",
         "🏢 פונדמנטלי:"]
    def pct(k):
        v = info.get(k)
        return f"{v:.0%}" if isinstance(v, (int, float)) else "אין נתון"
    cap = info.get("marketCap")
    L += [f"• סקטור: {qa['sector']}" + (f" | שווי שוק: {cap / 1e9:,.1f} מיליארד" if cap else ""),
          f"• מכפיל רווח: {info.get('trailingPE', 0):.1f}" if isinstance(info.get("trailingPE"), (int, float))
          else "• מכפיל רווח: אין נתון",
          f"• שולי רווח: {pct('profitMargins')} | תשואה להון: {pct('returnOnEquity')} | צמיחת הכנסות: {pct('revenueGrowth')}",
          f"• בדיקת האיכות של הבוט: {'✅ עוברת' if qa['ok'] else '❌ נכשלת ב' + ', '.join(qa['failed']) if qa['n'] else 'אין נתונים'}",
          f"• דוח כספי הבא: {f'בעוד {de} ימים' if de is not None else 'לא ידוע' + (' (בדוק במאיה)' if mkt == 'TA' else '')}"]
    tgt = info.get("targetMeanPrice")
    if isinstance(tgt, (int, float)) and info.get("numberOfAnalystOpinions"):
        L.append(f"• יעד ממוצע של {info['numberOfAnalystOpinions']} אנליסטים: {fmt(tgt, cur)} ({tgt / c - 1:+.0%})")
    rn = reddit_note(sym, reddit_map())
    if rn:
        L.append("• " + rn)

    L += ["", "🤖 דעת הבוט:"]
    if sig:
        names = {"pullback": "קנייה בתיקון", "breakout": "פריצה", "post_earnings": "תגובה חזקה לדוח"}
        stop = float(initial_stop(d, i))
        limit = c + LIMIT_ATR * float(row["atr"])
        _, _, equity = manual_snapshot(state, pos)
        per = (c - stop) * rate
        qty = int(min(equity * RISK_PER_TRADE / per, equity * MAX_POSITION_PCT / (c * rate), state["cash"] / (c * rate))) if per > 0 else 0
        L += [f"🎯 יש איתות היום: {names[sig]}.",
              f"אם היית פועל לפי הכללים: עד {qty} מניות, לא לקנות מעל {fmt(limit, cur)}, סטופ {fmt(stop, cur)} "
              f"(-{(c - stop) / c:.1%}), סיכון כ-{money(qty * per)}."]
        if not mkt_ok:
            L.append("⚠️ אבל השוק מתחת לממוצע 200, כך שהבוט עצמו לא היה קונה.")
        if sig != "post_earnings" and de is not None and de <= EARNINGS_BLACKOUT_DAYS:
            L.append(f"⚠️ אבל יש דוח בעוד {de} ימים, כך שהבוט עצמו היה מחכה.")
        if not qa["ok"] and qa["n"]:
            L.append("⚠️ אבל המניה נכשלת בבדיקת האיכות, כך שהבוט עצמו היה מדלג.")
    else:
        L.append(f"אין איתות כניסה היום. {verdict}.")
        L.append(f"אם תקנה בכל זאת, סטופ סביר לפי התנודתיות: כ-{fmt(c - ATR_STOP_MULT * float(row['atr']), cur)}.")
    if sym in pos:
        p = pos[sym]
        L.append(f"💼 בתיק שלך: {int(p['shares'])} מניות, ממוצע {fmt(p['avg_price'], cur)} ({c / p['avg_price'] - 1:+.1%}).")
    L += ["", "ניתוח אוטומטי לפי נתוני Yahoo, לא ייעוץ השקעות."]
    return "\n".join(L)


# ===================== דוחות =====================
def manual_snapshot(state, pos):
    rows, invested = [], 0.0
    for sym, p in pos.items():
        q = quote(sym)
        rate = to_ils_rate(p["currency"])
        if q and rate:
            val = p["shares"] * q[0] * rate
            rows.append((sym, p, q[0], val, val - p["cost_ils"]))
            invested += val
        else:
            rows.append((sym, p, None, p["cost_ils"], 0.0))
            invested += p["cost_ils"]
    return rows, invested, state["cash"] + invested


def bot_snapshot():
    """שווי תיק הבוט לפי היומן שלו ומחירים עדכניים."""
    try:
        st = json.load(open("logs/portfolio.json", encoding="utf-8"))
    except Exception:
        return None
    cash, value = float(st.get("cash", 0)), 0.0
    for r in read_csv("logs/paper_positions.csv"):
        sym = r["symbol"]
        q = quote(sym)
        rate = to_ils_rate(q[1]) if q else None
        if q and rate:
            value += float(r["shares"]) * q[0] * rate
        else:
            value += float(r["shares"]) * float(r["entry_price"]) * (0.01 if sym.endswith(".TA") else 3.7)
    return cash + value, float(st.get("start_capital", CAPITAL_ILS)), cash


def status_text(state, pos):
    rows, invested, equity = manual_snapshot(state, pos)
    total = equity / state["start_capital"] - 1
    trades = read_csv(TRADES)
    sells = [t for t in trades if t["side"] == "מכירה"]
    realized = sum(float(t["pnl_ils"] or 0) for t in sells)
    wins = sum(1 for t in sells if float(t["pnl_ils"] or 0) > 0)
    L = [f"👤 התיק שלי | {il_time()}",
         f"💼 שווי: {money(equity)} | תשואה: {equity - state['start_capital']:+,.0f} ₪ ({total:+.2%})",
         f"מזומן: {money(state['cash'])} | מושקע: {money(invested)} | פוזיציות: {len(pos)}",
         f"רווח ממומש: {realized:+,.0f} ₪ | עסקאות סגורות: {len(sells)}"
         + (f" | הצלחה {wins / len(sells):.0%}" if sells else "")]
    if rows:
        L.append("")
        for sym, p, px, val, pnl in rows:
            cur = p["currency"]
            if px is None:
                L.append(f"• {sym}: {int(p['shares'])} מניות | אין מחיר עדכני")
            else:
                L.append(f"{'🟢' if pnl >= 0 else '🔻'} {sym}: {int(p['shares'])} × {fmt(p['avg_price'], cur)} → "
                         f"{fmt(px, cur)} | {pnl / p['cost_ils']:+.1%} ({pnl:+,.0f} ₪)")
    if state.get("pending"):
        L += ["", "⏳ ממתינות לפתיחת המסחר: " + ", ".join(o["text"] for o in state["pending"])]
    b = bot_snapshot()
    if b:
        beq, bstart, _ = b
        L += ["", f"🤖 תיק הבוט: {money(beq)} ({beq / bstart - 1:+.2%})",
              f"🏁 {'אתה מוביל' if total > beq / bstart - 1 else 'הבוט מוביל' if total < beq / bstart - 1 else 'תיקו'}"]
    return "\n".join(L)


def summary_for_daily_report():
    """שורה לדוח היומי של הבוט."""
    if not os.path.exists(STATE):
        return None
    state, pos = load_state(), positions()
    return status_text(state, pos)


# ===================== ריצה =====================
def main():
    state, pos = load_state(), positions()
    out = []
    try:
        set_commands(state)
    except Exception as e:
        print("setMyCommands failed:", e)

    # 1. פקודות שממתינות לפתיחה
    still, filled = [], []
    for o in state.get("pending", []):
        txt, st = execute(o, state, pos)
        if st == "queued":
            still.append(o)
            continue
        filled.append(txt.split("\n")[0].replace("בוצע: ", "") + " | " + txt.split("\n")[1]
                      if st == "done" else f"{o['text']}: {txt}")
        append_csv(ORDERS, ORDER_F, {"received": o["received"], "command": o["text"], "status": st,
                                      "details": txt.replace("\n", " | ")})
    state["pending"] = still
    if filled:
        out.append(f"⏰ השוק נפתח. בוצעו {len(filled)} פקודות שהמתינו:\n" + "\n".join(filled)
                   + f"\nמזומן שנותר: {money(state['cash'])}")

    # 2. הודעות חדשות מטלגרם (רק מהצ'אט שלך)
    me = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    upd = tg("getUpdates", offset=state.get("offset", 0), timeout=0).get("result", []) \
        if os.environ.get("TELEGRAM_TOKEN") else []
    for u in upd:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != me or not msg.get("text"):
            continue
        text = msg["text"]
        rec = il_time(dt.datetime.fromtimestamp(msg.get("date", 0), dt.timezone.utc))
        o = parse(text)
        if o["cmd"] == "help":
            out.append(HELP)
            continue
        if o["cmd"] == "ask":
            out.append(o["text"])
            continue
        if o["cmd"] == "batch":
            out.append(run_batch(o, text, rec, state, pos))
            continue
        if o["cmd"] == "analyze":
            try:
                out.append(analyze_text(o["sym"], state, pos))
            except Exception as e:
                out.append(f"⚠️ הניתוח של {o['sym']} נכשל: {e}")
            continue
        if o["cmd"] == "status":
            out.append(status_text(state, pos))
            continue
        if o["cmd"] == "cancel":
            n = len(state["pending"])
            for p in state["pending"]:
                append_csv(ORDERS, ORDER_F, {"received": p["received"], "command": p["text"],
                                              "status": "cancelled", "details": "בוטלה על ידך"})
            state["pending"] = []
            out.append(f"🗑️ בוטלו {n} פקודות ממתינות." if n else "אין פקודות ממתינות.")
            continue
        if o["cmd"] == "usage":
            out.append("כתוב את הפקודה עם הסימבול, למשל:\n/buy TEVA.TA 100\n/buy AAPL 5000ש\"ח\n/sell TEVA.TA\n/analyze NVDA")
            continue
        if o["cmd"] == "unknown":
            out.append(f"🤔 לא הבנתי את \"{text}\". שלח עזרה לרשימת הפקודות.")
            continue
        o.update({"text": text, "received": rec})
        txt, st = execute(o, state, pos)
        if st == "queued":
            state["pending"].append(o)
        else:
            append_csv(ORDERS, ORDER_F, {"received": rec, "command": text, "status": st,
                                          "details": txt.replace("\n", " | ")})
        out.append(txt)

    save_positions(pos)
    _, _, equity = manual_snapshot(state, pos)
    state["peak"] = max(state.get("peak", equity), equity)
    save_state(state)
    for t in out:
        reply(t)


if __name__ == "__main__":
    main()
