"""בדיקה מהירה בלי התקנות: האם יש פקודות חדשות או ממתינות. 1 = יש עבודה."""
import json, os, sys, urllib.request
state = {}
if os.path.exists("logs/manual/state.json"):
    state = json.load(open("logs/manual/state.json", encoding="utf-8"))
if state.get("pending") or state.get("commands_v") != 2:
    print("work=1"); sys.exit(0)
tok = os.environ.get("TELEGRAM_TOKEN", "")
url = f"https://api.telegram.org/bot{tok}/getUpdates?offset={state.get('offset', 0)}&timeout=0"
try:
    n = len(json.load(urllib.request.urlopen(url, timeout=20)).get("result", []))
except Exception:
    n = 1
print("work=1" if n else "work=0")
