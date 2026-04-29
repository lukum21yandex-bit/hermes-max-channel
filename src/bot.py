"""
Hermes MAX Bot — core bot engine.

Handles longpoll updates, dispatches to user handlers.
"""
import asyncio
import logging
from typing import Callable, Optional, Coroutine
from dataclasses import dataclass

from .client import MaxClient, parse_update, UpdateType, MessageCreatedUpdate

logger = logging.getLogger(__name__)


@dataclass
class BotConfig:
    """Bot configuration."""
    token: str
    rate_limit_rps: float = 2.0
    longpoll_timeout: float = 30.0
    longpoll_limit: int = 50
    allowed_user_ids: Optional[list[int]] = None  # If set, only these users can interact


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
        self._update_handler: Optional[Callable[[any], Coroutine]] = None
        self._stopped = asyncio.Event()
        self._marker: Optional[int] = None
        self._stats = {"messages_received": 0, "messages_sent": 0, "errors": 0}

    def on_message(self, handler: Callable[[MessageCreatedUpdate], Coroutine]):
        """Register handler for text messages."""
        self._message_handler = handler
        return self

    def on_update(self, handler: Callable[[any], Coroutine]):
        """Register handler for all update types."""
        self._update_handler = handler
        return self

    async def start(self):
        """Initialize bot, fetch info."""
        me = await self.client.get_me()
        logger.info("Bot started: %s (id=%s)", me.get("name"), me.get("user_id"))
        logger.info("Rate limit: %.1f RPS", self.config.rate_limit_rps)

    async def _handle_update(self, update):
        """Dispatch update to appropriate handler."""
        try:
            logger.debug("Handling update: type=%s", update.update_type)
            if self._update_handler:
                await self._update_handler(update)
                return

            # Default: handle message_created
            from .client import MessageCreatedUpdate
            if isinstance(update, MessageCreatedUpdate):
                if self._message_handler:
                    logger.info("Calling message handler for user %s",
                                update.message.sender.user_id)
                    await self._message_handler(update)
                else:
                    # Default echo (debug)
                    text = update.message.body.text or "[без текста]"
                    sender = update.message.sender
                    logger.info("Received from %s (id=%s): %s",
                                sender.name, sender.user_id, text)
            else:
                logger.debug("Ignored update type: %s", update.update_type)
        except Exception as e:
            logger.exception("Error handling update: %s", e)
            self._stats["errors"] += 1

    async def run(self):
        """Main longpoll loop."""
        await self.start()

        logger.info("Starting longpoll loop (timeout=%.0fs, limit=%d)",
                    self.config.longpoll_timeout, self.config.longpoll_limit)

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
                            self._stats["messages_received"] += 1
                            await self._handle_update(update)

                        if updates.marker is not None:
                            self._marker = updates.marker
                            logger.debug("Marker advanced to %d", self._marker)
                    else:
                        # Timeout with no updates — just loop again
                        pass

                except asyncio.CancelledError:
                    logger.info("Cancelled, shutting down...")
                    break
                except Exception as e:
                    logger.exception("Loop error: %s", e)
                    self._stats["errors"] += 1
                    await asyncio.sleep(1)  # backoff on error

        finally:
            await self.client.close()
            logger.info("Bot stopped. Stats: %s", self._stats)

    def stop(self):
        """Signal the bot to stop."""
        self._stopped.set()
