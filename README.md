# Custom Emoji Bot 🎨

בוט טלגרם לניהול custom emoji עם מילות מפתח.

## התקנה

```bash
pip install -r requirements.txt
```

## הגדרת משתני סביבה

העתק את `.env.example` לקובץ `.env` ומלא:

```
BOT_TOKEN=...
ADMIN_ID=...   # ה-Telegram User ID שלך
```

ולהריץ:
```bash
BOT_TOKEN=xxx ADMIN_ID=12345 python bot.py
```

## שימוש

### מנהל
- שלח הודעה עם custom emoji ← הבוט ישאל למילות מפתח
- `/list` — רשימת כל האמוג'ים
- `/delete [id]` — מחיקה לפי ID
- `/cancel` — ביטול פעולה פעילה

### משתמשים
- שלח מילה / אימוג'י רגיל ← הבוט מחזיר את ה-custom emoji המתאים
- התאמה מדויקת מועדפת על חלקית
- אם אין כלום → "לא נמצא 🤷"

## מבנה emojis.json

```json
{
  "emojis": [
    {
      "id": 1,
      "custom_emoji_id": "5368324170671202286",
      "file_id": "",
      "keywords": ["שמח", "מאושר", "יפה"]
    }
  ],
  "next_id": 2
}
```
