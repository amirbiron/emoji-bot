import asyncio
import os
import logging
import threading
from flask import Flask, request as flask_request, jsonify
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


# ── Flask health / admin server ──────────────────────────────────────────────

flask_app = Flask(__name__)
flask_app.logger.setLevel(logging.WARNING)  # silence Flask request logs
logging.getLogger("werkzeug").setLevel(logging.WARNING)

LOG_ADMIN_TOKEN = os.getenv("LOG_ADMIN_TOKEN", "")


@flask_app.route("/")
@flask_app.route("/health")
def health():
    return "OK", 200


@flask_app.route("/admin/loglevel", methods=["POST"])
def set_loglevel():
    token = flask_request.headers.get("Authorization", "")
    if not LOG_ADMIN_TOKEN or token != f"Bearer {LOG_ADMIN_TOKEN}":
        return jsonify(error="unauthorized"), 401
    data = flask_request.get_json(silent=True) or {}
    name = data.get("logger", "root")
    level = data.get("level", "INFO").upper()
    numeric = getattr(logging, level, None)
    if numeric is None:
        return jsonify(error=f"unknown level: {level}"), 400
    logging.getLogger(name if name != "root" else None).setLevel(numeric)
    return jsonify(ok=True, logger=name, level=level)


def _start_flask():
    port = int(os.getenv("PORT", "10000"))
    thread = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()
    logger.info("Health-check server listening on port %s", port)


ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

WAITING_KEYWORDS = 1


# ── Storage helpers (MongoDB-backed) ─────────────────────────────────────────

def _emojis_col():
    import lock
    return lock.get_db()["emojis"]


def _counters_col():
    import lock
    return lock.get_db()["counters"]


def _next_id() -> int:
    """Atomically increment and return the next emoji id."""
    from pymongo import ReturnDocument
    doc = _counters_col().find_one_and_update(
        {"_id": "emoji_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return doc["seq"]


def add_emoji(custom_emoji_id: str, file_id: str, keywords: list[str]) -> int:
    entry_id = _next_id()
    _emojis_col().insert_one({
        "id": entry_id,
        "custom_emoji_id": custom_emoji_id,
        "file_id": file_id,
        "keywords": [k.strip().lower() for k in keywords],
    })
    return entry_id


def delete_emoji(entry_id: int) -> bool:
    result = _emojis_col().delete_one({"id": entry_id})
    return result.deleted_count > 0


def find_emoji(query: str):
    """
    Returns (entry, score) for the best match, or (None, 0).
    Exact keyword match = score 2, partial = score 1.
    """
    q = query.strip().lower()
    best_entry, best_score = None, 0

    for entry in _emojis_col().find():
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


def load_data() -> dict:
    """Return all emojis as a list (used by cmd_list)."""
    emojis = list(_emojis_col().find({}, {"_id": 0}))
    return {"emojis": emojis}


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
        "\u2b07\ufe0f \u05de\u05d4 \u05de\u05d9\u05dc\u05d5\u05ea \u05d4\u05de\u05e4\u05ea\u05d7 \u05dc\u05d0\u05de\u05d5\u05d2'\u05d9 \u05d4\u05d6\u05d4?\n"
        "\u05db\u05ea\u05d5\u05d1 \u05d0\u05d5\u05ea\u05df \u05de\u05d5\u05e4\u05e8\u05d3\u05d5\u05ea \u05d1\u05e4\u05e1\u05d9\u05e7, \u05dc\u05de\u05e9\u05dc: \u05e9\u05de\u05d7, \u05de\u05d0\u05d5\u05e9\u05e8, \u05d9\u05e4\u05d4"
    )
    return WAITING_KEYWORDS


async def admin_receive_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Admin sent keywords — save the entry."""
    text = update.message.text or ""
    keywords = [k.strip() for k in text.split(",") if k.strip()]

    if not keywords:
        await update.message.reply_text("\u26a0\ufe0f \u05dc\u05d0 \u05e7\u05d9\u05d1\u05dc\u05ea\u05d9 \u05de\u05d9\u05dc\u05d5\u05ea \u05de\u05e4\u05ea\u05d7. \u05e0\u05e1\u05d4 \u05e9\u05d5\u05d1.")
        return WAITING_KEYWORDS

    custom_emoji_id = ctx.user_data.get("pending_custom_emoji_id", "")
    entry_id = add_emoji(
        custom_emoji_id=custom_emoji_id,
        file_id="",
        keywords=keywords,
    )

    kw_display = ", ".join(keywords)
    await update.message.reply_text(
        f"\u2705 \u05e0\u05e9\u05de\u05e8! (ID: {entry_id})\n"
        f"\u05de\u05d9\u05dc\u05d5\u05ea \u05de\u05e4\u05ea\u05d7: {kw_display}"
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("\u274c \u05d1\u05d5\u05d8\u05dc.")
    return ConversationHandler.END


# ── Admin commands ────────────────────────────────────────────────────────────

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    data = load_data()
    emojis = data["emojis"]
    if not emojis:
        await update.message.reply_text("\u05d0\u05d9\u05df \u05d0\u05de\u05d5\u05d2'\u05d9\u05dd \u05e9\u05de\u05d5\u05e8\u05d9\u05dd \u05e2\u05d3\u05d9\u05d9\u05df.")
        return

    lines = []
    for e in emojis:
        kws = ", ".join(e["keywords"])
        lines.append(f"\U0001f194 {e['id']} | {kws}")

    await update.message.reply_text("\U0001f4cb \u05e8\u05e9\u05d9\u05de\u05ea \u05d0\u05de\u05d5\u05d2'\u05d9\u05dd:\n\n" + "\n".join(lines))


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("\u05e9\u05d9\u05de\u05d5\u05e9: /delete [id]")
        return
    entry_id = int(args[0])
    if delete_emoji(entry_id):
        await update.message.reply_text(f"\U0001f5d1\ufe0f \u05d0\u05de\u05d5\u05d2'\u05d9 {entry_id} \u05e0\u05de\u05d7\u05e7.")
    else:
        await update.message.reply_text(f"\u26a0\ufe0f \u05dc\u05d0 \u05e0\u05de\u05e6\u05d0 \u05d0\u05de\u05d5\u05d2'\u05d9 \u05e2\u05dd ID {entry_id}.")


# ── User handler ──────────────────────────────────────────────────────────────

async def user_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Any user sends text / regular emoji -> search and reply with custom emoji."""
    msg = update.message
    query = (msg.text or msg.caption or "").strip()
    if not query:
        return

    entry, score = find_emoji(query)
    if not entry:
        await msg.reply_text("\u05dc\u05d0 \u05e0\u05de\u05e6\u05d0 \U0001f937")
        return

    custom_emoji_id = entry["custom_emoji_id"]
    from telegram import MessageEntity
    emoji_char = "\U0001f60a"  # placeholder character; the entity overrides it visually
    # Telegram API uses UTF-16 offsets/lengths; emoji outside BMP = 2 UTF-16 units
    utf16_length = len(emoji_char.encode("utf-16-le")) // 2
    entity = MessageEntity(
        type="custom_emoji",
        offset=0,
        length=utf16_length,
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
    # Start health server first so Render sees an open port immediately.
    _start_flask()

    # Acquire distributed lock — blocks until we are the sole instance.
    import lock
    lock.acquire()

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

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    # Python 3.14 removed implicit event loop creation in get_event_loop().
    # Ensure a running loop exists before PTB's run_polling() needs one.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main()
