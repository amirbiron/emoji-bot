import asyncio
import os
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, Message
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Prevent httpx / httpcore from logging URLs that contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# ── Health-check server (keeps Render happy) ─────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):  # silence access logs
        pass


def _start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check server listening on port %s", port)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
EMOJIS_FILE = "emojis.json"

WAITING_KEYWORDS = 1


# ── Storage helpers ──────────────────────────────────────────────────────────

def load_data() -> dict:
    if not os.path.exists(EMOJIS_FILE):
        return {"emojis": [], "next_id": 1}
    with open(EMOJIS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict):
    with open(EMOJIS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_emoji(custom_emoji_id: str, file_id: str, keywords: list[str]) -> int:
    data = load_data()
    entry = {
        "id": data["next_id"],
        "custom_emoji_id": custom_emoji_id,
        "file_id": file_id,
        "keywords": [k.strip().lower() for k in keywords],
    }
    data["emojis"].append(entry)
    data["next_id"] += 1
    save_data(data)
    return entry["id"]


def delete_emoji(entry_id: int) -> bool:
    data = load_data()
    before = len(data["emojis"])
    data["emojis"] = [e for e in data["emojis"] if e["id"] != entry_id]
    if len(data["emojis"]) < before:
        save_data(data)
        return True
    return False


def find_emoji(query: str):
    """
    Returns (entry, score) for the best match, or (None, 0).
    Exact keyword match = score 2, partial = score 1.
    """
    data = load_data()
    q = query.strip().lower()
    best_entry, best_score = None, 0

    for entry in data["emojis"]:
        score = 0
        for kw in entry["keywords"]:
            if kw == q:
                score = max(score, 2)
            elif q in kw or kw in q:
                score = max(score, 1)
        if score > best_score:
            best_score = score
            best_entry = entry

    return best_entry, best_score


# ── Admin conversation: add emoji ────────────────────────────────────────────

async def admin_receive_emoji(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sent a message with custom emoji entities — ask for keywords."""
    msg: Message = update.message
    entities = msg.entities or []

    custom_emoji_entities = [
        e for e in entities if e.type == "custom_emoji"
    ]

    if not custom_emoji_entities:
        return ConversationHandler.END

    # Take the first custom emoji in the message
    entity = custom_emoji_entities[0]
    ctx.user_data["pending_custom_emoji_id"] = entity.custom_emoji_id
    ctx.user_data["pending_file_id"] = None  # custom emoji sticker file_id fetched later

    await msg.reply_text(
        "📝 מה מילות המפתח לאמוג'י הזה?\n"
        "כתוב אותן מופרדות בפסיק, למשל: שמח, מאושר, יפה"
    )
    return WAITING_KEYWORDS


async def admin_receive_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sent keywords — save the entry."""
    text = update.message.text or ""
    keywords = [k.strip() for k in text.split(",") if k.strip()]

    if not keywords:
        await update.message.reply_text("⚠️ לא קיבלתי מילות מפתח. נסה שוב.")
        return WAITING_KEYWORDS

    custom_emoji_id = ctx.user_data.get("pending_custom_emoji_id", "")
    entry_id = add_emoji(
        custom_emoji_id=custom_emoji_id,
        file_id="",
        keywords=keywords,
    )

    kw_display = ", ".join(keywords)
    await update.message.reply_text(
        f"✅ נשמר! (ID: {entry_id})\n"
        f"מילות מפתח: {kw_display}"
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ בוטל.")
    return ConversationHandler.END


# ── Admin commands ────────────────────────────────────────────────────────────

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_data()
    emojis = data["emojis"]
    if not emojis:
        await update.message.reply_text("אין אמוג'ים שמורים עדיין.")
        return

    lines = []
    for e in emojis:
        kws = ", ".join(e["keywords"])
        lines.append(f"🆔 {e['id']} | {kws}")

    await update.message.reply_text("📋 רשימת אמוג'ים:\n\n" + "\n".join(lines))


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("שימוש: /delete [id]")
        return
    entry_id = int(args[0])
    if delete_emoji(entry_id):
        await update.message.reply_text(f"🗑️ אמוג'י {entry_id} נמחק.")
    else:
        await update.message.reply_text(f"⚠️ לא נמצא אמוג'י עם ID {entry_id}.")


# ── User handler ──────────────────────────────────────────────────────────────

async def user_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Any user sends text / regular emoji → search and reply with custom emoji."""
    msg = update.message
    query = (msg.text or msg.caption or "").strip()
    if not query:
        return

    entry, score = find_emoji(query)
    if not entry:
        await msg.reply_text("לא נמצא 🤷")
        return

    custom_emoji_id = entry["custom_emoji_id"]
    # Reply with the custom emoji using its ID
    # Telegram supports sending custom emoji in text via the special syntax
    # We send it as a text message with the custom_emoji entity
    from telegram import MessageEntity
    emoji_char = "😊"  # placeholder character; the entity overrides it visually
    entity = MessageEntity(
        type="custom_emoji",
        offset=0,
        length=len(emoji_char),
        custom_emoji_id=custom_emoji_id,
    )
    await msg.reply_text(emoji_char, entities=[entity])


# ── Admin filter ──────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID


def has_custom_emoji(update: Update) -> bool:
    msg = update.message
    if not msg:
        return False
    entities = msg.entities or []
    return any(e.type == "custom_emoji" for e in entities)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Admin conversation for adding custom emoji
    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.User(ADMIN_ID) & filters.Entity("custom_emoji"),
                admin_receive_emoji,
            )
        ],
        states={
            WAITING_KEYWORDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_receive_keywords)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(admin_conv)
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("delete", cmd_delete))

    # User text queries (all users, including admin when not in conv)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Entity("custom_emoji"),
            user_query,
        )
    )

    _start_health_server()
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Python 3.14 removed implicit event loop creation in get_event_loop().
    # Ensure a running loop exists before PTB's run_polling() needs one.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
