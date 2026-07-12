"""
Max (max.ru) Platform Adapter for Hermes Agent.

Connects to the Russian messenger Max via its Platform API (platform-api.max.ru).
Uses webhook mode: the Hermes gateway registers an HTTP endpoint that
receives Max updates, and the Max REST API for outbound messages.

Supports text, images, audio, video, file attachments, typing indicators,
and access control (allow lists / open access).

Designed as a Hermes plugin — place in ``~/.hermes/plugins/platforms/max/``.
Zero changes to core Hermes code required.

Links:
    - API docs: https://dev.max.ru/docs-api
    - Max dev portal: https://dev.max.ru
    - Max messenger: https://max.ru

Configuration in config.yaml::

    gateway:
      platforms:
        max:
          enabled: true
          extra:
            token: your_bot_token_here
            webhook_url: https://your.domain/plugins/max/webhook
            allowed_users: []
            allow_all_users: false
            api_base_url: https://platform-api.max.ru

Environment variables (override YAML):

    MAX_TOKEN                  Bot token (required)
    MAX_WEBHOOK_URL            Public HTTPS URL for webhooks (required)
    MAX_ALLOWED_USERS          Comma-separated user IDs (optional)
    MAX_ALLOW_ALL_USERS        1/true/yes to allow everyone (optional)
    MAX_HOME_CHANNEL           Chat ID for cron delivery (optional)
    MAX_API_BASE_URL           API base URL (optional, dev/test)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Imports from the Hermes gateway (deferred — may not be importable at plugin
# discovery time when no gateway is running)
# ---------------------------------------------------------------------------

_gateway_base: Any = None
_gateway_config: Any = None
_gateway_session: Any = None
_MessageEvent: Any = None
_MessageType: Any = None
_SessionSource: Any = None
_SendResult: Any = None


def _ensure_gateway_imports() -> bool:
    """Lazy import of Hermes gateway base classes.

    Returns True on success, False if not available (e.g. tests).
    """
    global _gateway_base, _gateway_config, _gateway_session, _MessageEvent, _MessageType, _SessionSource, _SendResult
    if _gateway_base is not None:
        return True
    try:
        from gateway.platforms.base import (  # noqa: F401
            BasePlatformAdapter as _bpa,
            SendResult as _sr,
            MessageEvent as _me,
            MessageType as _mt,
        )
        from gateway.config import Platform as _p, PlatformConfig as _pc
        from gateway.session import SessionSource as _ss
        _gateway_base = _bpa
        _gateway_config = (_p, _pc)
        _gateway_session = _ss
        _MessageEvent = _me
        _MessageType = _mt
        _SessionSource = _ss
        _SendResult = _sr
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_API_BASE = "https://platform-api.max.ru"
MAX_MESSAGE_LENGTH = 4000
MAX_MEDIA_FETCH_BYTES = 50 * 1024 * 1024    # 50 MB
MAX_WEBHOOK_BODY_BYTES = 512 * 1024          # 512 KB
RETRY_ATTEMPTS = 20
RETRY_BASE_DELAY = 1                         # seconds (exponential backoff + jitter)

INTERESTING_UPDATE_TYPES = frozenset({"message_created", "message_callback"})

MEDIA_PLACEHOLDERS = {
    "image": "<media:image>",
    "video": "<media:video>",
    "audio": "<media:audio>",
    "file": "<media:document>",
}

MEDIA_DIRECTIVE_RE = re.compile(r"(?:^|\n)\s*MEDIA:\s*([^\n]+)\s*(?=\n|$)", re.IGNORECASE)
FEEDBACK_DIRECTIVE_RE = re.compile(r"(?:^|\n)\s*FEEDBACK:\s*(?=\n|$)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Max REST API Client
# ---------------------------------------------------------------------------


class MaxApiClient:
    """Async HTTP client for the Max Platform API.

    Covers all endpoints Hermes needs: send messages, send actions,
    manage webhooks (subscriptions), upload media, query bot info.

    Usage::

        api = MaxApiClient(token)
        info = await api.get_my_info()
        await api.send_message_to_user(12345, "Hello!")
    """

    def __init__(
        self,
        token: str,
        base_url: str = MAX_API_BASE,
        session: aiohttp.ClientSession | None = None,
    ):
        self._token = token
        self._base = base_url.rstrip("/")
        self._session = session
        self._own_session = session is None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def close(self) -> None:
        if self._own_session and self._session:
            await self._session.close()
            self._session = None

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": self._token, "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: Dict[str, Any] | None = None,
        body: Dict[str, Any] | None = None,
    ) -> Tuple[int, Any]:
        session = await self._ensure_session()
        url = f"{self._base}/{path.lstrip('/')}"
        params = {k: v for k, v in (query or {}).items() if v is not None}
        async with session.request(
            method, url, params=params, json=body, headers=self._headers(),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            status = resp.status
            try:
                data = await resp.json()
            except Exception:
                text = await resp.text()
                data = {"error": text[:500]}
            return status, data

    # ── Bot Info ──────────────────────────────────────────────────────

    async def get_my_info(self) -> Dict[str, Any] | None:
        status, data = await self._request("GET", "me")
        if status == 200 and isinstance(data, dict):
            return data
        logger.warning("Max get_my_info: HTTP %s — %s", status, _safe_str(data, 200))
        return None

    # ── Messages ──────────────────────────────────────────────────────

    async def send_message_to_user(
        self, user_id: int, text: str, extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        body: Dict[str, Any] = {"text": text or ""}
        if extra:
            body.update(extra)
        status, data = await self._request("POST", "messages", query={"user_id": user_id}, body=body)
        if status in (200, 201):
            return data
        logger.warning("Max send_message_to_user(%s): HTTP %s", user_id, status)
        return None

    async def send_message_to_chat(
        self, chat_id: int, text: str, extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        body: Dict[str, Any] = {"text": text or ""}
        if extra:
            body.update(extra)
        status, data = await self._request("POST", "messages", query={"chat_id": chat_id}, body=body)
        if status in (200, 201):
            return data
        logger.warning("Max send_message_to_chat(%s): HTTP %s", chat_id, status)
        return None

    async def send_message(
        self, recipient_id: int, text: str, chat_kind: str,
        extra: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | None:
        if chat_kind == "group":
            return await self.send_message_to_chat(recipient_id, text, extra)
        return await self.send_message_to_user(recipient_id, text, extra)

    async def send_message_with_attachments(
        self,
        recipient_id: int,
        text: str,
        chat_kind: str,
        attachments: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        body: Dict[str, Any] = {"text": text or None}
        if attachments:
            body["attachments"] = attachments
        if chat_kind == "group":
            return await self.send_message_to_chat(recipient_id, "", body)
        return await self.send_message_to_user(recipient_id, "", body)

    # ── Actions ───────────────────────────────────────────────────────

    async def send_action(self, chat_id: int, action: str) -> bool:
        status, data = await self._request(
            "POST", f"chats/{chat_id}/actions", body={"action": action},
        )
        return status in (200, 201)

    # ── Webhooks (subscriptions) ──────────────────────────────────────

    async def set_webhook(self, url: str) -> bool:
        """POST /subscriptions — register webhook URL.

        Max requires HTTPS (self-signed certs accepted).
        Body: {url, update_types: ["message_created"]}
        """
        body = {"url": url, "update_types": ["message_created", "message_callback"]}
        status, data = await self._request("POST", "subscriptions", body=body)
        if status in (200, 201):
            logger.info("Max: webhook registered → %s", url)
            return True
        logger.error("Max: webhook registration failed: HTTP %s — %s", status, _safe_str(data, 200))
        return False

    async def delete_webhook(self) -> bool:
        status, data = await self._request("DELETE", "subscriptions")
        if status in (200, 204):
            return True
        logger.warning("Max: delete_webhook HTTP %s", status)
        return False

    # ── Upload ─────────────────────────────────────────────────────────

    async def get_upload_url(self, media_type: str = "file") -> Dict[str, Any] | None:
        """POST /uploads?type=<type> → {url, token}."""
        status, data = await self._request("POST", "uploads", query={"type": media_type})
        if status in (200, 201) and isinstance(data, dict):
            return data
        logger.warning("Max: get_upload_url(%s) HTTP %s", media_type, status)
        return None

    async def upload_file_to_url(self, upload_url: str, data: bytes, filename: str = "file") -> bool:
        session = await self._ensure_session()
        form = aiohttp.FormData()
        form.add_field("data", data, filename=filename)
        try:
            async with session.post(upload_url, data=form, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                return resp.status in (200, 201, 204)
        except Exception as e:
            logger.warning("Max: upload to URL failed: %s", e)
            return False

    async def upload_media(self, media_type: str, data: bytes, filename: str = "file") -> str | None:
        """Upload a file to Max and return the upload token.

        Two-step flow:
          1. POST /uploads?type=<type> → {url, token}
          2. PUT/POST file to url

        Returns the token string, or None on failure.
        """
        info = await self.get_upload_url(media_type)
        if not info or not info.get("url"):
            return None
        ok = await self.upload_file_to_url(info["url"], data, filename)
        if ok:
            return info.get("token") or info.get("upload_token") or ""
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_str(data: Any, max_len: int = 200) -> str:
    """Truncate a value for safe logging."""
    s = str(data)
    return s[:max_len] + "..." if len(s) > max_len else s


def _is_max_id_positive(chat_id: int) -> bool:
    """Max convention: positive = user DM, negative = group chat."""
    return chat_id >= 0


def _parse_media_directives(text: str) -> Tuple[str, List[str]]:
    """Extract MEDIA:... directives from agent text.

    Returns (cleaned_text, list_of_urls).
    """
    urls = []
    cleaned = MEDIA_DIRECTIVE_RE.sub("", text)
    for m in MEDIA_DIRECTIVE_RE.finditer(text):
        url = m.group(1).strip().strip("\"'")
        if url:
            urls.append(url)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, urls


async def _fetch_media(url: str, session: aiohttp.ClientSession) -> Tuple[bytes | None, str]:
    """Download bytes from a URL. Returns (data, content_type)."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status != 200:
                return None, ""
            ct = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
            data = await resp.read()
            if len(data) > MAX_MEDIA_FETCH_BYTES:
                logger.warning("Max: media too large (%d bytes)", len(data))
                return None, ""
            return data, ct
    except Exception as e:
        logger.debug("Max: fetch error — %s", e)
        return None, ""


def _guess_media_type(url: str, content_type: str = "") -> str:
    ct = content_type.lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("audio/"):
        return "audio"
    if ct.startswith("video/"):
        return "video"
    path = url.split("?")[0].lower()
    if any(path.endswith(e) for e in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")):
        return "image"
    if any(path.endswith(e) for e in (".ogg", ".opus", ".mp3", ".m4a", ".wav", ".webm")):
        return "audio"
    if any(path.endswith(e) for e in (".mp4", ".mov", ".avi", ".mkv")):
        return "video"
    return "file"


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def validate_config(cfg: Any) -> List[str]:
    """Return a list of config errors (empty = valid)."""
    errors: List[str] = []
    _env_token = os.getenv("MAX_TOKEN", "") or ""
    _has_env_token = bool(_env_token.strip())
    _extra = getattr(cfg, "extra", {}) or {}
    _has_extra_token = bool(_extra.get("token", ""))
    _nested = _extra.get("extra", {})
    _has_nested = bool(isinstance(_nested, dict) and _nested.get("token", ""))
    logger.debug("Max env: env_token=%s extra_token=%s extra_keys=%s",
                 _has_env_token, _has_extra_token, list(_extra.keys()))
    _env_webhook = os.getenv("MAX_WEBHOOK_URL", "").strip()
    _extra_webhook = _extra.get("webhook_url", "")
    logger.debug("Max webhook: env=%s extra=%s",
                 "set" if _env_webhook else "missing",
                 "set" if _extra_webhook else "missing")
    token = _env_token.strip() or _extra.get("token", "")
    if not token:
        token = (
            os.getenv("MAX_TOKEN", "").strip()
            or _extra.get("token", "")
            or (isinstance(_extra.get("extra"), dict) and _extra["extra"].get("token", ""))
            or ""
        )
    if not token:
        errors.append("MAX_TOKEN is not set")
    webhook_url = (
        os.getenv("MAX_WEBHOOK_URL", "").strip()
        or _extra.get("webhook_url", "")
        or (isinstance(_extra.get("extra"), dict) and _extra["extra"].get("webhook_url", ""))
        or ""
    )
    if not webhook_url:
        errors.append("MAX_WEBHOOK_URL is not set")
    elif not webhook_url.startswith("https://"):
        errors.append(f"MAX_WEBHOOK_URL must use HTTPS (got {webhook_url})")
    logger.debug("Max validate_config: extra=%s errors=%d", list(_extra.keys()), len(errors))
    return len(errors) == 0


# ---------------------------------------------------------------------------
# Webhook handler builder
# ---------------------------------------------------------------------------


async def _build_webhook_handler(adapter: "MaxAdapter"):
    """Return an aiohttp handler for incoming Max webhook POSTs.

    The handler returns 200 immediately, then processes updates in the
    background (Max expects fast acknowledgment).
    """
    from aiohttp import web

    async def handler(request: web.Request) -> web.Response:
        if adapter._disconnecting:
            return web.json_response({"ok": False, "error": "shutting down"}, status=503)

        raw = await request.read()
        if len(raw) > MAX_WEBHOOK_BODY_BYTES:
            return web.json_response({"ok": False, "error": "payload too large"}, status=413)

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning("Max webhook: invalid JSON — %s", e)
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

        asyncio.ensure_future(adapter._process_updates(data))
        return web.json_response({"ok": True})

    return handler


# ---------------------------------------------------------------------------
# Env-based auto-enablement
# ---------------------------------------------------------------------------


def _env_enablement() -> Dict[str, Any] | None:
    """Seed PlatformConfig.extra from env vars.

    Called by the platform registry's env-enablement hook BEFORE adapter
    construction. Returns None when MAX_TOKEN isn't set; the caller skips
    auto-enabling.

    The ``home_channel`` key is handled specially by the core — it becomes
    a proper ``HomeChannel`` dataclass on the ``PlatformConfig`` rather
    than being merged into ``extra``.
    """
    token = os.getenv("MAX_TOKEN", "").strip()
    if not token:
        return None
    seed: Dict[str, Any] = {
        "token": token,
    }
    webhook_url = os.getenv("MAX_WEBHOOK_URL", "").strip()
    if webhook_url:
        seed["webhook_url"] = webhook_url
    allowed = os.getenv("MAX_ALLOWED_USERS", "").strip()
    if allowed:
        seed["allowed_users"] = [u.strip() for u in allowed.split(",") if u.strip()]
    if os.getenv("MAX_ALLOW_ALL_USERS", "").lower() in {"1", "true", "yes"}:
        seed["allow_all_users"] = True
    api_base = os.getenv("MAX_API_BASE_URL", "").strip()
    if api_base:
        seed["api_base_url"] = api_base
    home = os.getenv("MAX_HOME_CHANNEL", "").strip() or None
    if home:
        seed["home_channel"] = home
    return seed


# ---------------------------------------------------------------------------
# Standalone sender (for cron jobs that run outside the gateway)
# ---------------------------------------------------------------------------


async def _standalone_send(
    chat_id: str,
    text: str,
    *,
    token: str | None = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Send a message to Max without a running gateway adapter.

    Used by the cron scheduler when ``deliver=max`` is specified and the
    gateway process is not running.
    """
    resolved_token = token or os.getenv("MAX_TOKEN", "").strip()
    if not resolved_token:
        return {"ok": False, "error": "MAX_TOKEN not set"}
    api = MaxApiClient(resolved_token)
    try:
        cid = int(chat_id)
        chat_kind = "group" if cid < 0 else "direct"
        clean, _ = _parse_media_directives(text)
        result = await api.send_message(cid, clean, chat_kind)
        if result:
            return {"ok": True, "channel": "max", "message_id": f"max-{int(time.time())}"}
        return {"ok": False, "channel": "max", "error": "send failed"}
    except Exception as e:
        return {"ok": False, "channel": "max", "error": str(e)}
    finally:
        await api.close()


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------


def _is_connected(adapter: "MaxAdapter") -> bool:
    return adapter._connected


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# MaxAdapter
# ---------------------------------------------------------------------------


class MaxAdapter:
    """Hermes gateway adapter for the Max messenger.

    Uses webhook mode: Max sends message events to an HTTPS endpoint served
    by the Hermes gateway (``/plugins/max/webhook``). Outbound messages
    use the Max REST API directly.

    This is a ``BasePlatformAdapter`` subclass integrated via the Hermes
    plugin system.

    Key flows:
        - **Inbound:** Max → webhook POST → ``_process_updates`` →
          ``_handle_message_created`` → gateway ``handle_inbound_message``
        - **Outbound:** ``send()`` → ``MaxApiClient.send_message()`` /
          ``send_message_with_attachments()``
        - **Media uploads:** Agent returns ``MEDIA: <url>`` directives →
          download → upload to Max → attach token → send
    """

    # ---- Hermes Platform API -------------------------------------------------

    name = "Max"
    platform = "max"
    max_message_length = MAX_MESSAGE_LENGTH

    def __init__(self, cfg: Any, **kwargs: Any):
        if not _ensure_gateway_imports():
            raise RuntimeError("Max plugin requires Hermes gateway base classes")

        self._extra: Dict[str, Any] = getattr(cfg, "extra", {}) or {}
        self._cfg = cfg
        self._gateway = kwargs.get("gateway")

        # Resolve config (env > YAML)
        self._token: str = os.getenv("MAX_TOKEN", "").strip() or self._extra.get("token", "")
        self._webhook_url: str = (
            os.getenv("MAX_WEBHOOK_URL", "").strip() or self._extra.get("webhook_url", "")
        )
        self._api_base: str = (
            os.getenv("MAX_API_BASE_URL", "").strip() or self._extra.get("api_base_url", MAX_API_BASE)
        )

        # Access control
        raw_allowed: Any = os.getenv("MAX_ALLOWED_USERS", "").strip() or self._extra.get("allowed_users", [])
        if isinstance(raw_allowed, str):
            self._allowed_users: set = {u.strip() for u in raw_allowed.split(",") if u.strip()}
        else:
            self._allowed_users = set(raw_allowed or [])
        self._allow_all: bool = (
            os.getenv("MAX_ALLOW_ALL_USERS", "").lower() in {"1", "true", "yes"}
            or self._extra.get("allow_all_users", False)
        )

        # Runtime state
        self._api: MaxApiClient | None = None
        self._session: aiohttp.ClientSession | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._connected: bool = False
        self._disconnecting: bool = False
        self._bot_user_id: int | None = None
        self._bot_info: Dict[str, Any] | None = None
        # Reply threading: track last message IDs per chat (incoming user msg → reply to it,
        # outgoing bot msg → user can reply to it)
        self._last_bot_message_id: Dict[str, str] = {}    # chat_id → last bot message mid
        self._last_user_message_id: Dict[str, str] = {}   # chat_id → last user message mid

        # Webhook HTTP server (aiohttp)
        self._webhook_listen: str = (
            os.getenv("MAX_WEBHOOK_LISTEN", "127.0.0.1").strip()
            or self._extra.get("webhook_listen", "127.0.0.1")
        )
        self._webhook_port: int = int(
            os.getenv("MAX_WEBHOOK_PORT", "9642").strip()
            or self._extra.get("webhook_port", 9642)
        )
        self._webhook_app: Any = None  # aiohttp.web.Application
        self._webhook_runner: Any = None  # aiohttp.web.AppRunner
        self._webhook_site: Any = None  # aiohttp.web.TCPSite

        # Gateway callback slots (set by set_message_handler, set_session_store, etc.)
        self._message_handler: Any = None
        self._busy_session_handler: Any = None
        self._session_store: Any = None
        self._fatal_error_handler: Any = None
        self._pending_messages: Dict[str, Any] = {}

    @property
    def name(self) -> str:
        return "Max"

    @property
    def platform(self) -> str:
        return "max"

    @property
    def max_message_length(self) -> int:
        return MAX_MESSAGE_LENGTH

    # ── Gateway callback registration ──────────────────────────────────

    async def handle_message(self, event: Any) -> None:
        """Handle an incoming message event (required for session resumption).

        Called by the gateway for session resumption after restart.
        The event must be a ``MessageEvent`` with a ``SessionSource``.
        """
        if self._message_handler:
            await self._message_handler(event)
        else:
            logger.warning("Max: handle_message called but no handler set")

    def set_message_handler(self, handler: Any) -> None:
        """Set the handler for inbound messages from the gateway."""
        self._message_handler = handler

    def set_busy_session_handler(self, handler: Any) -> None:
        """Set handler for messages arriving during active sessions."""
        self._busy_session_handler = handler

    def set_session_store(self, session_store: Any) -> None:
        """Set the session store for persisting conversations."""
        self._session_store = session_store

    def get_pending_message(self, session_key: str) -> Any | None:
        """Return and remove a pending (interrupt) message for this session."""
        return self._pending_messages.pop(session_key, None)

    def set_fatal_error_handler(self, handler: Any) -> None:
        """Set the handler for fatal adapter errors."""
        self._fatal_error_handler = handler

    def _set_fatal_error(self, code: str, msg: str, retryable: bool = False) -> None:
        """Report a fatal error via the handler, if set."""
        logger.error("Max fatal error: [%s] %s (retryable=%s)", code, msg, retryable)
        if self._fatal_error_handler:
            self._fatal_error_handler(code, msg, retryable)

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self) -> bool:
        """Verify token, register webhook, start heartbeat."""
        if not self._token:
            logger.error("Max: token missing — set MAX_TOKEN")
            return False
        if not self._webhook_url:
            logger.error("Max: webhook_url missing — set MAX_WEBHOOK_URL")
            return False

        self._session = aiohttp.ClientSession()
        self._api = MaxApiClient(self._token, base_url=self._api_base, session=self._session)

        # Verify token
        info = await self._api.get_my_info()
        if not info:
            logger.error("Max: token verification failed")
            await self._cleanup()
            return False

        self._bot_info = info
        self._bot_user_id = info.get("user_id")
        logger.info(
            "Max: authenticated — bot=%s user_id=%s",
            info.get("name", "?"), self._bot_user_id,
        )

        # Register webhook with Max
        if not await self._api.set_webhook(self._webhook_url):
            logger.error("Max: webhook registration failed")
            await self._cleanup()
            return False

        # Start local webhook HTTP server
        if not await self._start_webhook_server():
            logger.error("Max: failed to start webhook HTTP server")
            await self._cleanup()
            return False

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self._connected = True
        logger.info("Max: connected (webhook mode)")
        return True

    async def disconnect(self) -> None:
        self._disconnecting = True
        self._connected = False
        # Stop heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        # Stop webhook HTTP server
        await self._stop_webhook_server()

        # Best-effort webhook cleanup
        if self._api:
            try:
                await self._api.delete_webhook()
            except Exception:
                pass

        await self._cleanup()
        logger.info("Max: disconnected")

    async def _cleanup(self) -> None:
        if self._api:
            await self._api.close()
            self._api = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _heartbeat_loop(self) -> None:
        while not self._disconnecting:
            await asyncio.sleep(30)
            logger.debug("Max: heartbeat")

    # ── Webhook HTTP server ────────────────────────────────────────────

    async def _start_webhook_server(self) -> bool:
        """Start an aiohttp web server to receive Max webhook POSTs."""
        from aiohttp import web

        self._webhook_app = web.Application()
        handler = await _build_webhook_handler(self)

        # Register webhook route — Max posts updates here
        self._webhook_app.router.add_post(
            "/plugins/max/webhook", handler,
        )
        # Also accept root-level health check
        self._webhook_app.router.add_get("/health", lambda r: web.json_response({"ok": True}))

        self._webhook_runner = web.AppRunner(self._webhook_app, handle_signals=False)
        await self._webhook_runner.setup()
        self._webhook_site = web.TCPSite(
            self._webhook_runner, self._webhook_listen, self._webhook_port,
        )
        try:
            await self._webhook_site.start()
            logger.info(
                "Max: webhook HTTP server listening on %s:%s",
                self._webhook_listen, self._webhook_port,
            )
            # Verify the port is actually listening
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = s.connect_ex((self._webhook_listen, self._webhook_port))
            s.close()
            if result != 0:
                logger.error("Max: webhook server start returned OK but port %s:%s is NOT listening (err=%d)",
                             self._webhook_listen, self._webhook_port, result)
                return False
            return True
        except Exception as e:
            logger.error("Max: failed to start webhook server on %s:%s — %s",
                         self._webhook_listen, self._webhook_port, e)
            return False

    async def _stop_webhook_server(self) -> None:
        """Stop the aiohttp webhook server."""
        if self._webhook_site:
            try:
                await self._webhook_site.stop()
            except Exception:
                pass
            self._webhook_site = None
        if self._webhook_runner:
            try:
                await self._webhook_runner.cleanup()
            except Exception:
                pass
            self._webhook_runner = None
        self._webhook_app = None

    # ── Webhook processing ────────────────────────────────────────────────

    async def _process_updates(self, data: Any) -> None:
        if self._disconnecting:
            return
        updates = data if isinstance(data, list) else [data]
        for update in updates:
            if not isinstance(update, dict):
                continue
            utype = update.get("update_type")
            if utype not in INTERESTING_UPDATE_TYPES:
                continue
            try:
                if utype == "message_callback":
                    await self._handle_callback(update)
                else:
                    await self._handle_message(update)
            except Exception as e:
                logger.exception("Max: update error: %s", e)

    async def _handle_callback(self, update: Dict[str, Any]) -> None:
        """Process a ``message_callback`` update — user pressed an inline button."""
        msg = update.get("message") or {}
        sender = msg.get("sender", {}) or {}
        sender_id = self._resolve_user_id(sender)
        if sender_id is not None and sender_id == self._bot_user_id:
            return

        # Extract callback payload
        callback_data = update.get("callback") or {}
        payload = callback_data.get("payload") or update.get("payload") or ""
        # Max sends the original message mid in update.message
        orig_text = (msg.get("text") or "").strip()

        # Map payload to feedback
        feedback_map = {
            "fb:like": "👍 (пользователь доволен ответом)",
            "fb:dislike": "👎 (пользователь недоволен ответом)",
        }
        feedback = feedback_map.get(payload, payload)

        logger.info("Max: callback received — payload=%s from user=%s", payload, sender_id)
        logger.info("Max: feedback=%s on message: %s", feedback, orig_text[:200])

        # Store feedback for potential future use
        chat_id = str(msg.get("recipient", {}).get("chat_id") or sender_id or "")
        if chat_id and not hasattr(self, '_feedback_log'):
            self._feedback_log = {}
        if chat_id:
            self._feedback_log[chat_id] = {"payload": payload, "feedback": feedback, "timestamp": time.time()}

    async def _handle_message(self, update: Dict[str, Any]) -> None:
        """Process a single ``message_created`` update."""
        msg = update.get("message")
        if not msg:
            return

        sender = msg.get("sender", {}) or {}
        recipient = msg.get("recipient", {}) or {}

        # Skip self-messages
        sender_id = self._resolve_user_id(sender)
        if sender_id is not None and sender_id == self._bot_user_id:
            return

        # Extract content
        text, image_urls, audio_urls = self._extract_content(msg)
        has_attachments = bool(msg.get("body", {}).get("attachments"))
        if not text and not has_attachments:
            return

        # Reply-to context: if user replied to a message, extract the original text
        linked_msg = msg.get("link") or {}
        if linked_msg and isinstance(linked_msg, dict):
            link_type = linked_msg.get("type", "")
            # Max API: link.message.text holds the original message text
            link_message = linked_msg.get("message", {}) or {}
            link_text = (link_message.get("text") or "").strip()
            link_sender = linked_msg.get("sender", {}) or {}
            link_sender_name = link_sender.get("name") or link_sender.get("username", "")
            is_bot_msg = link_sender.get("is_bot", False)

            if link_text:
                if link_type == "reply":
                    who = "your message" if is_bot_msg else f"a message by {link_sender_name}"
                    quoted = f'[The user replied to {who}: "{link_text[:300]}"]\n\n'
                else:
                    quoted = f'[The user forwarded a message by {link_sender_name}: "{link_text[:300]}"]\n\n'
                text = quoted + text
                logger.info("Max: reply-to context added (type=%s, orig_text=%d chars)", link_type, len(link_text))

        # Download and cache audio files for STT transcription
        audio_paths: List[str] = []
        audio_types: List[str] = []
        if audio_urls:
            _ensure_gateway_imports()
            for au_url in audio_urls:
                try:
                    from gateway.platforms.base import cache_audio_from_url
                    cached = await cache_audio_from_url(au_url, ext=".ogg")
                    audio_paths.append(cached)
                    audio_types.append("audio/ogg")
                    logger.info("Max: cached user audio at %s", cached)
                except Exception as e:
                    logger.warning("Max: failed to cache audio: %s", e, exc_info=True)

        # Determine chat type and IDs
        chat_kind = self._resolve_chat_kind(recipient)
        from_id = str(sender_id) if sender_id else ""
        to_id = self._resolve_reply_id(recipient, chat_kind, from_id)

        if not to_id or not from_id:
            logger.debug("Max: skip — missing sender/recipient id")
            return

        # Access control
        if not self._is_allowed(from_id):
            logger.debug("Max: user %s not allowed", from_id)
            return

        to_num = int(to_id)

        # Track user's message ID for reply threading (bot will reply to this message)
        user_mid = msg.get("mid")
        if user_mid:
            self._last_user_message_id[str(to_num)] = str(user_mid)
            # Also update the bot reply target to the user's latest message
            self._last_bot_message_id[str(to_num)] = str(user_mid)

        # Send typing indicator
        await self._safe_action(to_num, "typing_on")

        # Build session source
        source = self._build_source(chat_kind, from_id, to_id, sender, recipient)

        # Build and dispatch message event
        event = {
            "chat_id": to_id,
            "user_id": from_id,
            "text": text,
            "chat_kind": chat_kind,
            "source": source,
            "_raw_message": msg,
        }

        # Ensure gateway base classes are imported
        _ensure_gateway_imports()

        # Dispatch to the gateway through the message handler installed by
        # set_message_handler().  The handler expects a MessageEvent object.
        if self._message_handler and _MessageEvent and _MessageType:
            try:
                me = _MessageEvent(
                    text=text,
                    message_type=_MessageType.TEXT,
                    source=source,
                    message_id=f"max-{int(time.time() * 1000)}",
                    timestamp=__import__("datetime").datetime.now(),
                    media_urls=audio_paths,
                    media_types=audio_types,
                )
                response_text = await self._message_handler(me)
                # Send the agent's response back to the Max user
                if response_text and isinstance(response_text, str) and response_text.strip():
                    logger.debug("Max: agent response (%d chars), sending to %s", len(response_text), to_id)
                    await self.send(to_id, response_text)
            except Exception as e:
                logger.exception("Max: message handler error: %s", e)
        else:
            logger.warning("Max: no message handler — echo fallback")
            await self.send(to_id, f"Echo: {text[:200]}")

    def _resolve_user_id(self, sender: Dict[str, Any]) -> int | None:
        uid = sender.get("user_id") or sender.get("id")
        if uid is not None:
            try:
                return int(uid)
            except (ValueError, TypeError):
                pass
        return None

    def _resolve_reply_id(
        self, recipient: Dict[str, Any], chat_kind: str, from_id: str,
    ) -> str | None:
        """Determine where to send the reply.

        - Group chats: reply to the chat (chat_id, usually negative).
        - Direct messages: reply to the sender (user_id, usually positive).
        """
        if chat_kind == "group":
            cid = recipient.get("chat_id") or recipient.get("id")
            if cid is not None:
                return str(cid)
            chat = recipient.get("chat", {}) or {}
            cid = chat.get("chat_id") or chat.get("id")
            if cid is not None:
                return str(cid)
            return None
        # DM → reply to the sender
        return from_id

    def _resolve_chat_kind(self, recipient: Dict[str, Any]) -> str:
        ct = (recipient.get("chat_type") or recipient.get("type", "")).lower()
        if ct in ("chat", "channel", "group"):
            return "group"
        cid = recipient.get("chat_id")
        if cid is not None:
            try:
                if int(cid) < 0:
                    return "group"
            except (ValueError, TypeError):
                pass
        return "direct"

    def _extract_content(self, msg: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
        """Extract text, image URLs, and audio URLs from a message.

        Returns (text, image_urls, audio_urls).
        """
        body = msg.get("body", {}) or {}
        text = (body.get("text") or "").strip()
        attachments = body.get("attachments", []) or []
        image_urls: List[str] = []
        audio_urls: List[str] = []
        placeholders: List[str] = []

        for att in attachments:
            if not isinstance(att, dict):
                continue
            att_type = att.get("type", "file")
            payload = att.get("payload", {}) or {}
            url = payload.get("url", "")
            placeholders.append(MEDIA_PLACEHOLDERS.get(att_type, "<media:document>"))
            if att_type == "image" and url:
                image_urls.append(url)
            elif att_type == "audio" and url:
                audio_urls.append(url)

        parts = [text] + placeholders if text else placeholders
        return "\n".join(parts).strip(), image_urls, audio_urls

    def _build_source(
        self, chat_kind: str, from_id: str, to_id: str,
        sender: Dict[str, Any], recipient: Dict[str, Any],
    ) -> Any:
        """Build a SessionSource for the gateway message handler."""
        _ensure_gateway_imports()
        sender_name = sender.get("name") or sender.get("username", "")
        chat_title = recipient.get("title") or recipient.get("name", "")
        if not chat_title and chat_kind == "direct":
            chat_title = sender_name or f"user_{from_id}"
        if _SessionSource is not None and _gateway_config is not None:
            _plat, _ = _gateway_config
            return _SessionSource(
                platform=_plat("max"),
                chat_id=to_id,
                chat_name=chat_title or to_id,
                chat_type=chat_kind,
                user_id=from_id,
                user_name=sender_name,
            )
        return {"platform": "max", "chat_id": to_id, "user_id": from_id}

    def _is_allowed(self, user_id: str) -> bool:
        if self._allow_all:
            return True
        if not self._allowed_users:
            return True
        return user_id in self._allowed_users

    # ── Actions ──────────────────────────────────────────────────────────

    async def _safe_action(self, chat_id: int, action: str) -> None:
        if not self._api:
            return
        try:
            await self._api.send_action(chat_id, action)
        except Exception:
            pass

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        try:
            await self._safe_action(int(chat_id), "typing_on")
        except (ValueError, TypeError):
            pass

    # ── Sending messages ──────────────────────────────────────────────────

    # Inline feedback buttons attached to every bot response
    _FEEDBACK_KEYBOARD = [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[
                {"type": "callback", "text": "👍", "payload": "fb:like"},
                {"type": "callback", "text": "👎", "payload": "fb:dislike"},
            ]]
        },
    }]

    async def send(
        self, chat_id: str, text: str = "",
        reply_options: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any] | None:
        _ensure_gateway_imports()
        if not self._api or self._disconnecting:
            return None

        # Gateway may pass text as ``content`` keyword arg or as positional arg.
        if not text and "content" in kwargs:
            text = kwargs.pop("content", "")
        text = str(text or "")

        if not self._api or self._disconnecting:
            return None

        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            logger.warning("Max: invalid chat_id %s", chat_id)
            return None

        chat_kind = "group" if cid < 0 else "direct"
        clean_text, media_urls = _parse_media_directives(text)

        # Check for FEEDBACK: directive — agent requests feedback buttons
        has_feedback = bool(FEEDBACK_DIRECTIVE_RE.search(text))
        if has_feedback:
            clean_text = FEEDBACK_DIRECTIVE_RE.sub("", clean_text)
            clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()

        if media_urls:
            attachments = await self._prepare_attachments(media_urls)
            if attachments:
                if has_feedback:
                    attachments.extend(self._FEEDBACK_KEYBOARD)
                result = await self._api.send_message_with_attachments(
                    cid, clean_text, chat_kind, attachments,
                )
                if result:
                    msg_obj = result.get("message", {}) or {}
                    mid = msg_obj.get("mid") if isinstance(msg_obj, dict) else result.get("mid")
                    if mid:
                        self._last_bot_message_id[str(cid)] = str(mid)
                    return _SendResult(success=True, message_id=mid or f"max-{int(time.time())}")

        # Text-only send
        extra: Dict[str, Any] = {}
        last_msg_id = self._last_bot_message_id.get(str(cid))
        if last_msg_id:
            extra["link"] = {"mid": last_msg_id}
        if has_feedback:
            extra["attachments"] = list(self._FEEDBACK_KEYBOARD)
        result = await self._api.send_message(cid, clean_text, chat_kind, extra)
        if result:
            msg_obj = result.get("message", {}) or {}
            mid = msg_obj.get("mid") if isinstance(msg_obj, dict) else result.get("mid")
            if mid:
                self._last_bot_message_id[str(cid)] = str(mid)
            return _SendResult(success=True, message_id=mid or f"max-{int(time.time())}")

        return _SendResult(success=False, error="send failed")

    async def _prepare_attachments(self, urls: List[str]) -> List[Dict[str, Any]]:
        if not self._api or not self._session:
            return []
        attachments: List[Dict[str, Any]] = []
        for url in urls:
            if not url:
                continue
            data, ct = await _fetch_media(url, self._session)
            if data is None or len(data) == 0:
                continue
            mtype = _guess_media_type(url, ct)
            if mtype == "image":
                # Images: send URL directly
                attachments.append({"type": "image", "payload": {"url": url}})
            else:
                token = await self._api.upload_media(mtype, data)
                if token:
                    attachments.append({"type": mtype, "payload": {"token": token}})
        return attachments

    async def edit_message(
        self, chat_id: str, message_id: str, text: str = "",
        reply_options: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> bool:
        """Edit a sent message. Max does not support editing — no-op."""
        return True

    async def send_image(
        self, chat_id: str, image_url: str, caption: str = "",
    ) -> Dict[str, Any] | None:
        return await self._send_media(chat_id, caption or None, [{"type": "image", "payload": {"url": image_url},"desc": "image"}])

    async def send_document(
        self, chat_id: str, file_path: str, caption: str = "",
    ) -> Dict[str, Any] | None:
        if not self._api:
            return None
        try:
            import aiofiles
            async with aiofiles.open(file_path, "rb") as f:
                data = await f.read()
        except ImportError:
            with open(file_path, "rb") as f:
                data = f.read()
        except Exception as e:
            logger.warning("Max: read file error — %s", e)
            return None
        if len(data) > MAX_MEDIA_FETCH_BYTES:
            logger.warning("Max: file too large: %s", file_path)
            return None
        token = await self._api.upload_media("file", data)
        if not token:
            return None
        return await self._send_media(
            chat_id, caption or None,
            [{"type": "file", "payload": {"token": token}}],
        )

    async def _send_media(
        self, chat_id: str, text: str | None,
        attachments: List[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        if not self._api:
            return None
        try:
            cid = int(chat_id)
        except (ValueError, TypeError):
            return None
        chat_kind = "group" if cid < 0 else "direct"
        result = await self._api.send_message_with_attachments(cid, text or "", chat_kind, attachments)
        if result:
            return {"ok": True, "message_id": f"max-media-{int(time.time())}"}
        return None

    # ── Chat info ─────────────────────────────────────────────────────────

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "name": self._bot_info.get("name", f"max_{chat_id}") if self._bot_info else f"max_{chat_id}",
            "type": "dm" if int(chat_id) >= 0 else "group",
            "chat_id": chat_id,
        }


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Called by the Hermes plugin loader.

    Registers the Max platform adapter with the gateway.
    """
    ctx.register_platform(
        name="max",
        label="Max",
        adapter_factory=lambda cfg, **kw: MaxAdapter(cfg, **kw),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=_is_connected,
        required_env=["MAX_TOKEN", "MAX_WEBHOOK_URL"],
        install_hint="pip install aiohttp (included with Hermes gateway)",
        setup_fn=None,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MAX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="MAX_ALLOWED_USERS",
        allow_all_env="MAX_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via Max (max.ru), a Russian messenger. "
            "Max supports plain text and images. "
            "To send images or other media, include the URL on its own line "
            "prefixed with MEDIA: (e.g. MEDIA:https://example.com/photo.jpg). "
            "To attach feedback buttons (👍/👎) to your message — only for "
            "final results or summaries where user feedback is valuable — "
            "include FEEDBACK: on its own line. Do NOT add FEEDBACK: to "
            "progress messages, tool outputs, or intermediate steps. "
            "Keep responses concise and conversational."
        ),
    )
    logger.info("Max platform plugin registered")
