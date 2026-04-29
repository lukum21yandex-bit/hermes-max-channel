"""
Hermes AI Worker — processes inbox messages and generates replies.

Runs alongside the MAX bot, reading from data/inbox.json and writing
responses to data/outbox.json. The bot then sends those responses back
to users via MAX API.
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import uuid

from .file_utils import FileLock, atomic_write_json, locked_read_json as _read_locked, locked_write_json as _write_locked

logger = logging.getLogger("hermes_worker")

INBOX_PATH = Path("data/inbox.json")
OUTBOX_PATH = Path("data/outbox.json")
PROCESSED_PATH = Path("data/inbox_processed.json")  # archive
PROCESSED_JSON_PATH = Path("data/processed.json")  # bot's outbox archive

# File locks (co-located)
INBOX_LOCK = INBOX_PATH.with_suffix(".lock")
OUTBOX_LOCK = OUTBOX_PATH.with_suffix(".lock")
INBOX_PROC_LOCK = Path("data/inbox_processed.lock")

# Simple advisory lock for worker single-instance
WORKER_LOCK = Path("data/.worker_lock")


class InboxMessage:
    """Schema for inbox message."""
    def __init__(
        self,
        user_id: int,
        username: str,
        text: str,
        mid: str,
        chat_id: int,
        timestamp: float,
        reply_sent: bool = False,
    ):
        self.user_id = user_id
        self.username = username
        self.text = text
        self.mid = mid
        self.chat_id = chat_id
        self.timestamp = timestamp
        self.reply_sent = reply_sent
        self.id = str(uuid.uuid4())

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "text": self.text,
            "mid": self.mid,
            "chat_id": self.chat_id,
            "timestamp": self.timestamp,
            "reply_sent": self.reply_sent,
            "processed_at": None,
        }


class OutboxMessage:
    """Schema for outbox (reply) message."""
    def __init__(
        self,
        user_id: int,
        text: str,
        mid: str,
        chat_id: int,
        reply_to: Optional[str] = None,
    ):
        self.user_id = user_id
        self.text = text
        self.mid = mid
        self.chat_id = chat_id
        self.reply_to = reply_to  # original message mid to reply to
        self.id = str(uuid.uuid4())
        self.created_at = time.time()
        self.sent = False

    def to_dict(self):
        d = {
            "id": self.id,
            "user_id": self.user_id,
            "text": self.text,
            "mid": self.mid,
            "chat_id": self.chat_id,
            "created_at": self.created_at,
            "sent": self.sent,
        }
        if self.reply_to:
            d["reply_to"] = self.reply_to
        return d


# FileLock instance for single-worker guarantee
_worker_lock: Optional[FileLock] = None


def _acquire_lock() -> bool:
    """Acquire exclusive worker lock (single instance guarantee)."""
    global _worker_lock
    try:
        _worker_lock = FileLock(WORKER_LOCK, timeout=2)
        _worker_lock.acquire()
        return True
    except Exception:
        return False


def _release_lock():
    global _worker_lock
    if _worker_lock:
        try:
            _worker_lock.release()
        except Exception:
            pass
        _worker_lock = None


def _read_json(path: Path, default=None):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error("Read error %s: %s", path, e)
    return default or []


def _write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class HermesWorker:
    """Background worker that reads inbox, generates replies, writes outbox."""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self._running = False

    async def start(self):
        """Start worker (alias for run, for compatibility)."""
        await self.run()

    async def run(self):
        """Main worker loop."""
        self._running = True
        logger.info("HermesWorker started")
        try:
            while self._running:
                try:
                    await self._process_inbox()
                    await asyncio.sleep(self.poll_interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.exception("Worker error: %s", e)
                    await asyncio.sleep(2)
        finally:
            self._running = False
            logger.info("HermesWorker stopped")

    def stop(self):
        """Stop worker gracefully."""
        self._running = False

    async def _process_inbox(self):
        """Read inbox, generate replies for new messages."""
        if not _acquire_lock():
            return

        try:
            # Read inbox under lock
            inbox = _read_locked(INBOX_PATH, INBOX_LOCK, default=[])
            if not inbox:
                return

            new_msgs = [m for m in inbox if not m.get("reply_sent")]
            if not new_msgs:
                return

            logger.info("Processing %d new inbox messages", len(new_msgs))

            sent_to_archive = []

            for msg_dict in new_msgs:
                try:
                    msg = InboxMessage(
                        user_id=msg_dict["user_id"],
                        username=msg_dict.get("username", ""),
                        text=msg_dict["text"],
                        mid=msg_dict["mid"],
                        chat_id=msg_dict.get("chat_id") or msg_dict["user_id"],
                        timestamp=msg_dict.get("timestamp", time.time()),
                        reply_sent=False,
                    )

                    reply_text = await self._generate_reply(msg)

                    # Write reply to outbox under lock, with reply_to=original mid
                    out_msg = OutboxMessage(
                        user_id=msg.user_id,
                        text=reply_text,
                        mid=msg.mid,
                        chat_id=msg.chat_id,
                        reply_to=msg.mid,  # reply to the incoming message
                    )
                    outbox = _read_locked(OUTBOX_PATH, OUTBOX_LOCK, default=[])
                    outbox.append(out_msg.to_dict())
                    _write_locked(OUTBOX_PATH, OUTBOX_LOCK, outbox)
                    logger.info(
                        "Queued reply for user %s (mid=%s): %s",
                        msg.username or msg.user_id,
                        msg.mid,
                        reply_text[:60],
                    )

                    # Mark inbox message as processed (will be archived below)
                    msg_dict["reply_sent"] = True
                    msg_dict["processed_at"] = time.time()
                    msg_dict["reply_mid"] = out_msg.id
                    sent_to_archive.append(msg_dict)
                except Exception as e:
                    logger.exception("Failed to process inbox msg: %s", e)

            # Write updated inbox (marking processed)
            _write_locked(INBOX_PATH, INBOX_LOCK, inbox)

            # Archive processed inbox messages (keep inbox lean)
            if sent_to_archive:
                with FileLock(INBOX_PROC_LOCK, timeout=5):
                    processed_inbox = _read_locked(PROCESSED_PATH, PROCESSED_LOCK, default=[])
                    processed_inbox.extend(sent_to_archive)
                    atomic_write_json(PROCESSED_PATH, processed_inbox)
                logger.info("Archived %d processed inbox messages", len(sent_to_archive))

        finally:
            _release_lock()
    async def _generate_reply(self, msg: InboxMessage) -> str:
        """
        Generate reply using Hermes's internal LLM context.

        Since we're running inside the same agent, we simulate LLM
        via string template. To use real LLM, integrate with ollama,
        LM Studio, or external API.
        """
        user = msg.username or f"user_{msg.user_id}"
        # Build prompt for LLM
        prompt = (
            f"Пользователь {user} в MAX написал:\n"
            f"\"{msg.text}\"\n\n"
            "Напиши краткий дружелюбный ответ на русском (до 100 слов)."
        )
        logger.debug("Generated prompt for %s: %s", user, prompt[:80])

        # Placeholder: replace with actual LLM call
        # For now, simple contextual response
        return f"Привет, {user}! Я — Hermes AI. Получил ваше сообщение: «{msg.text}». Чем могу помочь?"
