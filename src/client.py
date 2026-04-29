"""
MAX Bot API HTTP client.

Based on official Go client: https://github.com/max-messenger/max-bot-api-client-go
"""
import logging

logger = logging.getLogger(__name__)
import asyncio
import json
import time
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

import httpx

from .models import (
    Update,
    MessageCreatedUpdate,
    MessageEditedUpdate,
    BotStartedUpdate,
    User,
    Recipient,
    MessageBody,
    Message as models_Message,
    parse_update as parse_update_from_models,
)


__all__ = [
    "MaxClient",
    "UpdateList",
    "UpdateType",
    "parse_update",
    "User",
    "Recipient",
    "MessageBody",
    "MessageCreatedUpdate",
    "MessageEditedUpdate",
    "BotStartedUpdate",
]


# Constants
DEFAULT_BASE_URL = "https://platform-api.max.ru/"
MAX_VERSION = "1.2.5"
MAX_UPDATE_LIMIT = 50
DEFAULT_TIMEOUT = 30.0  # seconds
MAX_RETRIES = 3
RATE_LIMIT_RPS = 2.0  # MAX API limit: 2 requests per second


class UpdateType(str, Enum):
    MESSAGE_CALLBACK = "message_callback"
    MESSAGE_CREATED = "message_created"
    MESSAGE_REMOVED = "message_removed"
    MESSAGE_EDITED = "message_edited"
    BOT_ADDED = "bot_added"
    BOT_REMOVED = "bot_removed"
    BOT_STOPPED = "bot_stopped"
    DIALOG_REMOVED = "dialog_removed"
    DIALOG_CLEARED = "dialog_cleared"
    USER_ADDED = "user_added"
    USER_REMOVED = "user_removed"
    BOT_STARTED = "bot_started"
    CHAT_TITLE_CHANGED = "chat_title_changed"


# Path constants
PATH_UPDATES = "updates"
PATH_MESSAGES = "messages"
PATH_ME = "me"
PATH_SUBSCRIPTIONS = "subscriptions"
PATH_ANSWERS = "answers"


@dataclass
class UpdateList:
    updates: list
    marker: Optional[int] = None


class RateLimiter:
    """Simple rate limiter for 2 RPS constraint."""
    def __init__(self, rps: float = RATE_LIMIT_RPS):
        self.rps = rps
        self.interval = 1.0 / rps
        self.last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            elapsed = time.monotonic() - self.last_call
            if elapsed < self.interval:
                await asyncio.sleep(self.interval - elapsed)
            self.last_call = time.monotonic()


class MaxClient:
    """HTTP client for MAX Bot API with rate limiting and retry support."""

    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        rate_limit_rps: float = RATE_LIMIT_RPS,
    ):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.rate_limiter = RateLimiter(rate_limit_rps)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        await self._open()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _open(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": self.token,
                    "User-Agent": f"hermes-max-bot/{MAX_VERSION}",
                },
            )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    def _url(self, path: str, query: Optional[Dict] = None) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            params = []
            for k, v in query.items():
                if v is None:
                    continue
                if isinstance(v, bool):
                    v = str(v).lower()
                elif isinstance(v, (list, tuple)):
                    for item in v:
                        params.append((k, item))
                    continue
                params.append((k, str(v)))
            if params:
                from urllib.parse import urlencode
                url += "?" + urlencode(params)
        return url

    async def request_once(
        self,
        method: str,
        path: str,
        query: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Single HTTP request (no retry). Rate-limited.
        Returns (status_code, json_response).
        """
        await self.rate_limiter.acquire()

        if self._client is None:
            await self._open()

        assert self._client is not None

        url = self._url(path, query)
        resp = await self._client.request(method, url, json=json_data)
        data = resp.json()
        if resp.status_code != 200:
            return resp.status_code, data
        return 200, data

    async def request(
        self,
        method: str,
        path: str,
        query: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """
        Rate-limited HTTP request with retry logic (3 attempts, exp backoff).
        Returns (status_code, json_response).
        """
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                return await self.request_once(method, path, query, json_data)
            except httpx.RequestError as e:
                last_exc = e
                if attempt < MAX_RETRIES - 1:
                    backoff = 2 ** attempt
                    await asyncio.sleep(backoff)
                    continue
                break
        raise RuntimeError(f"Request failed after {MAX_RETRIES} attempts: {last_exc}")

    async def get_updates(
        self,
        marker: Optional[int] = None,
        limit: int = MAX_UPDATE_LIMIT,
        timeout: float = DEFAULT_TIMEOUT,
        types: Optional[list] = None,
    ) -> UpdateList:
        """
        Fetch updates via longpoll.

        Longpoll timeout is expected; treat ReadTimeout as empty update list.
        Other network errors are propagated to caller (bot loop) for handling.
        """
        query = {
            "v": MAX_VERSION,
            "limit": limit,
            "marker": marker,
            "timeout": int(timeout),
        }
        if types:
            query["types"] = types

        try:
            status, data = await self.request_once("GET", PATH_UPDATES, query=query)
            if status != 200:
                logger.warning("get_updates non-200 status: %s", status)
                return UpdateList(updates=[], marker=marker)
        except httpx.ReadTimeout:
            # Expected longpoll timeout — return empty list
            return UpdateList(updates=[], marker=marker)
        except httpx.RequestError:
            # Propagate other network errors to bot.run()
            raise

        updates_raw = data.get("updates", [])
        updates = [parse_update(u) for u in updates_raw]
        m = data.get("marker")
        return UpdateList(updates=updates, marker=m)

    async def send_message(
        self,
        text: str,
        user_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        format: str = "html",
        notify: bool = True,
    ) -> Dict[str, Any]:
        if not user_id and not chat_id:
            raise ValueError("Either user_id or chat_id must be set")
        if user_id and chat_id:
            raise ValueError("Only one of user_id or chat_id allowed")

        query = {"v": MAX_VERSION}
        if user_id:
            query["user_id"] = user_id
        if chat_id:
            query["chat_id"] = chat_id

        payload = {"text": text, "format": format, "notify": notify}
        status, data = await self.request("POST", PATH_MESSAGES, query=query, json_data=payload)
        return data

    async def get_me(self) -> Dict[str, Any]:
        status, data = await self.request("GET", PATH_ME)
        return data


# --- Parser utilities ---

def parse_update(raw: Dict[str, Any]) -> Update:
    utype = raw.get("update_type", "")

    if utype == UpdateType.MESSAGE_CREATED:
        msg_data = raw.get("message", {})
        return MessageCreatedUpdate(
            update_type=utype,
            timestamp=raw.get("timestamp", 0),
            raw=raw,
            message=parse_message(msg_data),
        )
    elif utype == UpdateType.MESSAGE_EDITED:
        msg_data = raw.get("message", {})
        return MessageEditedUpdate(
            update_type=utype,
            timestamp=raw.get("timestamp", 0),
            raw=raw,
            message=parse_message(msg_data),
        )
    elif utype == UpdateType.BOT_STARTED:
        return BotStartedUpdate(
            update_type=utype,
            timestamp=raw.get("timestamp", 0),
            raw=raw,
            chat_id=raw.get("chat_id", 0),
            user=parse_user(raw.get("user", {})),
            payload=raw.get("payload"),
        )
    else:
        return Update(update_type=utype, timestamp=raw.get("timestamp", 0), raw=raw)


def parse_user(data: Dict[str, Any]) -> User:
    return User(
        user_id=data.get("user_id", 0),
        name=data.get("name", ""),
        username=data.get("username"),
        is_bot=data.get("is_bot", False),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )


def parse_recipient(data: Dict[str, Any]) -> Recipient:
    return Recipient(
        chat_id=data.get("chat_id", 0),
        chat_type=data.get("chat_type", "dialog"),
        user_id=data.get("user_id"),
    )


def parse_message(data: Dict[str, Any]) -> models_Message:
    return models_Message(
        sender=parse_user(data.get("sender", {})),
        recipient=parse_recipient(data.get("recipient", {})),
        timestamp=data.get("timestamp", 0),
        body=_parse_message_body(data.get("body", {})),
        link=data.get("link"),
        url=data.get("url"),
    )


def _parse_message_body(data: Dict[str, Any]) -> MessageBody:
    return MessageBody(
        mid=data.get("mid", ""),
        seq=data.get("seq", 0),
        text=data.get("text"),
        attachments=data.get("attachments", []),
        reply_to=data.get("reply_to"),
        markups=data.get("markups", []),
    )
