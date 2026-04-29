"""
Hermes MAX Bot — core bot engine.

Handles longpoll updates, dispatches to user handlers.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional, Coroutine, Any
from dataclasses import dataclass

from .client import MaxClient, parse_update, UpdateType, MessageCreatedUpdate, User

logger = logging.getLogger(__name__)

# File paths
INBOX_PATH = Path("data/inbox.json")
OUTBOX_PATH = Path("data/outbox.json")


@dataclass
class BotConfig:
    """Bot configuration."""
    token: str
    rate_limit_rps: float = 2.0
    longpoll_timeout: float = 30.0
    longpoll_limit: int = 50
    allowed_user_ids: Optional[list[int]] = None  # If set, only these users can interact


def _read_json(path: Path, default=None):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("Read error %s: %s", path, e)
    return default or []


def _append_json(path: Path, item: dict):
    data = _read_json(path, default=[])
    data.append(item)
    _write_json(path, data)


def _write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


class HermesMaxBot:
    """
    Main bot class.

    Usage:
        bot = HermesMaxBot(token="...")
        bot.on_message(handle_message)
        await bot.run()
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self.client = MaxClient(
            token=config.token,
            rate_limit_rps=config.rate_limit_rps,
        )
        self._message_handler: Optional[Callable[[MessageCreatedUpdate], Coroutine]] = None
        self._update_handler: Optional[Callable[[Any], Coroutine]] = None
        self._stopped = asyncio.Event()
        self._marker: Optional[int] = None
        self._stats = {"messages_received": 0, "messages_sent": 0, "errors": 0}

        # Ensure data dir exists
        Path("data").mkdir(exist_ok=True)

    def on_message(self, handler: Callable[[MessageCreatedUpdate], Coroutine]):
        """Register handler for text messages."""
        self._message_handler = handler
        return self

    def on_update(self, handler: Callable[[Any], Coroutine]):
        """Register handler for all update types."""
        self._update_handler = handler
        return self

    async def start(self):
        """Initialize bot, fetch info."""
        me = await self.client.get_me()
        logger.info("Bot started: %s (id=%s)", me.get("name"), me.get("user_id"))
        logger.info("Rate limit: %.1f RPS", self.config.rate_limit_rps)

        # Ensure inbox/outbox files exist
        if not INBOX_PATH.exists():
            _write_json(INBOX_PATH, [])
        if not OUTBOX_PATH.exists():
            _write_json(OUTBOX_PATH, [])

    async def _handle_update(self, update):
        """Dispatch update to appropriate handler."""
        try:
            logger.debug("Handling update: type=%s", update.update_type)
            if self._update_handler:
                await self._update_handler(update)
                return

            # Default: handle message_created
            if isinstance(update, MessageCreatedUpdate):
                self._stats["messages_received"] += 1
                msg = update.message
                sender = msg.sender
                text = msg.body.text or "[без текста]"

                logger.info("Received from %s (id=%s): %s",
                            sender.name, sender.user_id, text)

                # Write to inbox for Hermes worker
                self._write_to_inbox(msg)

                # Try to send any available reply from outbox immediately
                await self._process_outbox_for_user(sender.user_id, msg.recipient.chat_id)
            else:
                logger.debug("Ignored update type: %s", update.update_type)
        except Exception as e:
            logger.exception("Error handling update: %s", e)
            self._stats["errors"] += 1

    def _write_to_inbox(self, msg):
        """Append incoming message to inbox queue."""
        item = {
            "user_id": msg.sender.user_id,
            "username": msg.sender.username or msg.sender.name,
            "text": msg.body.text or "",
            "mid": msg.body.mid,
            "chat_id": msg.recipient.chat_id,
            "timestamp": time.time(),
            "reply_sent": False,
            "processed_at": None,
        }
        _append_json(INBOX_PATH, item)
        logger.debug("Wrote to inbox: user=%s mid=%s", item["user_id"], item["mid"])

    async def _process_outbox(self):
        """Check outbox and send pending replies."""
        try:
            outbox = _read_json(OUTBOX_PATH, default=[])
            if not outbox:
                return

            pending = [m for m in outbox if not m.get("sent")]
            if not pending:
                return

            logger.info("Outbox: %d pending replies", len(pending))

            for reply in pending:
                try:
                    user_id = reply["user_id"]
                    text = reply["text"]
                    mid = reply.get("mid")  # optional: reply to specific message

                    # Send via MAX API
                    await self.client.send_message(text=text, user_id=user_id)
                    logger.info("Sent reply to user %s: %s", user_id, text[:50])

                    # Mark as sent
                    reply["sent"] = True
                    reply["sent_at"] = time.time()
                    self._stats["messages_sent"] += 1

                    # Remove from outbox after send? Keep for audit.
                    # For now, keep but mark sent.
                except Exception as e:
                    logger.error("Failed to send reply to %s: %s", reply.get("user_id"), e)
                    self._stats["errors"] += 1

            # Write updated outbox
            _write_json(OUTBOX_PATH, outbox)

        except Exception as e:
            logger.exception("Outbox processing error: %s", e)

    async def _process_outbox_for_user(self, user_id: int, chat_id: int):
        """Send any pending replies for a specific user (fast path)."""
        try:
            outbox = _read_json(OUTBOX_PATH, default=[])
            pending = [m for m in outbox if not m.get("sent") and m.get("user_id") == user_id]
            if not pending:
                return

            for reply in pending:
                text = reply["text"]
                await self.client.send_message(text=text, user_id=user_id)
                reply["sent"] = True
                reply["sent_at"] = time.time()
                self._stats["messages_sent"] += 1
                logger.info("Sent reply to user %s: %s", user_id, text[:50])

            _write_json(OUTBOX_PATH, outbox)
        except Exception as e:
            logger.exception("Outbox send error: %s", e)

    async def _outbox_loop(self):
        """Background task: periodically flush outbox."""
        logger.info("Outbox processor started")
        while not self._stopped.is_set():
            try:
                await self._process_outbox()
                await asyncio.sleep(1.0)  # check every second
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception("Outbox loop error: %s", e)
                await asyncio.sleep(2)
        logger.info("Outbox processor stopped")

    async def run(self):
        """Main bot loop with outbox co-task."""
        await self.start()

        logger.info("Starting longpoll + outbox processor (timeout=%.0fs, limit=%d)",
                    self.config.longpoll_timeout, self.config.longpoll_limit)

        # Start outbox background task
        outbox_task = asyncio.create_task(self._outbox_loop())

        try:
            while not self._stopped.is_set():
                try:
                    updates = await self.client.get_updates(
                        marker=self._marker,
                        limit=self.config.longpoll_limit,
                        timeout=self.config.longpoll_timeout,
                    )

                    if updates.updates:
                        for update in updates.updates:
                            await self._handle_update(update)

                        if updates.marker is not None:
                            self._marker = updates.marker
                            logger.debug("Marker advanced to %d", self._marker)
                    # else: timeout — continue
                except asyncio.CancelledError:
                    logger.info("Cancelled, shutting down...")
                    break
                except Exception as e:
                    logger.exception("Loop error: %s", e)
                    self._stats["errors"] += 1
                    await asyncio.sleep(1)
        finally:
            outbox_task.cancel()
            try:
                await outbox_task
            except asyncio.CancelledError:
                pass
            await self.client.close()
            logger.info("Bot stopped. Stats: %s", self._stats)

    def stop(self):
        """Signal the bot to stop."""
        self._stopped.set()
