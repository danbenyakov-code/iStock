"""פרשנות אנליסט בבינה מלאכותית. פרשנות בלבד: לא משנה אף החלטה."""
import os
import requests
from config import USE_AI_COMMENTARY, AI_MODEL

SYSTEM = """אתה אנליסט מניות בכיר שכותב פרשנות יומית קצרה לתיק מסחר שמנוהל על ידי מערכת כללים אוטומטית.

חוקים מחייבים:
- ההחלטות כבר התקבלו על ידי המערכת. אתה לא משנה אותן ולא נותן פקודות. אתה מפרש ומעיר.
- הסתמך אך ורק על הנתונים שסופקו. אין לך גישה לחדשות. אסור להמציא אירועים, חדשות, מחירים או מספרים.
- אם אתה חושב שפעולה של המערכת חלשה או מסוכנת, אמור זאת בכנות ונמק לפי הנתונים.
- כתוב בעברית, תמציתי ומקצועי, עד 350 מילים. אל תשתמש במקף ארוך.

מבנה:
1. תמונת מצב: משפט או שניים על התיק והשוק.
2. הפעולות של היום והפקודות למחר: האם כל אחת נראית סבירה, ומה הסיכון העיקרי בה.
3. מתלבט: 1 עד 3 מניות מרשימת "קרובה לאיתות", מה צריך לקרות כדי שייכנסו, ומה מדאיג בהן.
4. נקודה אחת לתשומת לב לימים הקרובים."""


def commentary(report, research_md):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not USE_AI_COMMENTARY or not key:
        return None, None
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 1500, "system": SYSTEM,
                  "messages": [{"role": "user", "content":
                                f"דוח התיק היום:\n\n{report}\n\nמחברת המחקר:\n\n{research_md}"}]},
            timeout=120)
        data = r.json()
        if r.status_code != 200:
            return None, f"פרשנות AI נכשלה: {data.get('error', {}).get('message', r.status_code)}"
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text.strip() or None, None
    except Exception as e:
        return None, f"פרשנות AI נכשלה: {e}"
