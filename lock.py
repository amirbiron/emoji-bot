"""
Distributed lock using MongoDB with TTL-based lease and heartbeat.

Ensures only one bot instance polls Telegram at a time, even across
Render deploys where old and new instances overlap.
"""

import atexit
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

# ── Config from ENV ──────────────────────────────────────────────────────────

SERVICE_ID = os.getenv("SERVICE_ID", "emoji-bot")
INSTANCE_ID = os.getenv("RENDER_INSTANCE_ID", f"local-{os.getpid()}")
SERVICE_NAME = os.getenv("RENDER_SERVICE_NAME", "emoji-bot")
MONGODB_URI = os.getenv("MONGODB_URI", "")

LOCK_LEASE_SECONDS = int(os.getenv("LOCK_LEASE_SECONDS", "60"))
_default_hb = max(5, int(LOCK_LEASE_SECONDS * 0.4))
LOCK_HEARTBEAT_INTERVAL = int(os.getenv("LOCK_HEARTBEAT_INTERVAL", str(_default_hb)))

LOCK_WAIT_FOR_ACQUIRE = os.getenv("LOCK_WAIT_FOR_ACQUIRE", "false").lower() == "true"
LOCK_ACQUIRE_MAX_WAIT = int(os.getenv("LOCK_ACQUIRE_MAX_WAIT", "0"))
LOCK_WAIT_MIN_SECONDS = int(os.getenv("LOCK_WAIT_MIN_SECONDS", "15"))
LOCK_WAIT_MAX_SECONDS = int(os.getenv("LOCK_WAIT_MAX_SECONDS", "45"))

COLLECTION_NAME = "bot_locks"

# ── Internal state ───────────────────────────────────────────────────────────

_heartbeat_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None
_owns_lock = False
_collection = None


def _now():
    return datetime.now(timezone.utc)


def _expiry():
    return _now() + timedelta(seconds=LOCK_LEASE_SECONDS)


def _get_collection():
    global _collection
    if _collection is not None:
        return _collection
    client = MongoClient(MONGODB_URI)
    try:
        db = client.get_default_database()
    except Exception:
        db = client[os.getenv("MONGODB_DB", "emoji_bot")]
    col = db[COLLECTION_NAME]
    # Ensure TTL index so orphaned locks expire automatically.
    col.create_index("expiresAt", expireAfterSeconds=0)
    _collection = col
    return col


# ── Heartbeat ────────────────────────────────────────────────────────────────

def _heartbeat_loop():
    """Periodically refresh the lease. Exit the process if we lose ownership."""
    col = _get_collection()
    while not _heartbeat_stop.is_set():
        _heartbeat_stop.wait(LOCK_HEARTBEAT_INTERVAL)
        if _heartbeat_stop.is_set():
            break
        try:
            result = col.update_one(
                {"_id": SERVICE_ID, "owner": INSTANCE_ID},
                {"$set": {"expiresAt": _expiry(), "updatedAt": _now()}},
            )
            if result.matched_count == 0:
                logger.warning(
                    "Lost lock ownership (instance=%s). Exiting.", INSTANCE_ID
                )
                os._exit(0)
        except Exception:
            logger.exception("Heartbeat failed")


def _start_heartbeat():
    global _heartbeat_thread
    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    _heartbeat_thread.start()


def _stop_heartbeat():
    _heartbeat_stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=5)


# ── Acquire / release ────────────────────────────────────────────────────────

def _try_acquire_once() -> bool:
    """Attempt to grab the lock. Returns True on success."""
    global _owns_lock
    col = _get_collection()
    now = _now()
    doc = {
        "_id": SERVICE_ID,
        "owner": INSTANCE_ID,
        "host": SERVICE_NAME,
        "expiresAt": _expiry(),
        "createdAt": now,
        "updatedAt": now,
    }
    # Try insert (no existing lock).
    try:
        col.insert_one(doc)
        _owns_lock = True
        return True
    except DuplicateKeyError:
        pass

    # Lock exists — try to take over if it expired or we already own it.
    result = col.update_one(
        {
            "_id": SERVICE_ID,
            "$or": [
                {"expiresAt": {"$lte": now}},
                {"owner": INSTANCE_ID},
            ],
        },
        {
            "$set": {
                "owner": INSTANCE_ID,
                "host": SERVICE_NAME,
                "expiresAt": _expiry(),
                "updatedAt": now,
            }
        },
    )
    if result.modified_count > 0:
        _owns_lock = True
        return True

    return False


def acquire() -> bool:
    """
    Acquire the distributed lock.

    Behaviour depends on LOCK_WAIT_FOR_ACQUIRE:
    - false (default): passive wait with random backoff, then retry once.
    - true: active retry loop until acquired or timeout.

    Returns True when the lock is acquired. May call sys.exit(0) if it
    decides to give up cleanly.
    """
    if _try_acquire_once():
        logger.info("Lock acquired (instance=%s).", INSTANCE_ID)
        _start_heartbeat()
        return True

    if LOCK_WAIT_FOR_ACQUIRE:
        return _wait_active()
    else:
        return _wait_passive()


def _wait_passive() -> bool:
    """Sleep a random interval, then retry in a loop."""
    while True:
        delay = random.uniform(LOCK_WAIT_MIN_SECONDS, LOCK_WAIT_MAX_SECONDS)
        logger.info(
            "Lock held by another instance. Waiting %.0fs before retry…", delay
        )
        time.sleep(delay)
        if _try_acquire_once():
            logger.info("Lock acquired after passive wait (instance=%s).", INSTANCE_ID)
            _start_heartbeat()
            return True


def _wait_active() -> bool:
    """Retry with short intervals until acquired or timeout."""
    deadline = time.monotonic() + LOCK_ACQUIRE_MAX_WAIT if LOCK_ACQUIRE_MAX_WAIT > 0 else None
    attempt = 0
    while True:
        attempt += 1
        if deadline and time.monotonic() >= deadline:
            logger.warning("Lock acquire timeout after %ds. Exiting.", LOCK_ACQUIRE_MAX_WAIT)
            sys.exit(0)
        wait = min(2 ** attempt, 30) + random.uniform(0, 1)
        logger.info("Lock held. Retry #%d in %.1fs…", attempt, wait)
        time.sleep(wait)
        if _try_acquire_once():
            logger.info("Lock acquired after active wait (instance=%s).", INSTANCE_ID)
            _start_heartbeat()
            return True


def release():
    """Release the lock if we own it."""
    global _owns_lock
    if not _owns_lock:
        return
    _stop_heartbeat()
    try:
        col = _get_collection()
        result = col.delete_one({"_id": SERVICE_ID, "owner": INSTANCE_ID})
        if result.deleted_count:
            logger.info("Lock released (instance=%s).", INSTANCE_ID)
    except Exception:
        logger.exception("Failed to release lock")
    _owns_lock = False


# ── Cleanup hooks ────────────────────────────────────────────────────────────

def _sigterm_handler(signum, frame):
    release()
    sys.exit(0)


atexit.register(release)
signal.signal(signal.SIGTERM, _sigterm_handler)
