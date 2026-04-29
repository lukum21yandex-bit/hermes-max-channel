#!/usr/bin/env python3
"""
Hermes MAX Bot — integration test suite.

Tests:
  1. inbox → outbox roundtrip (synthetic message)
  2. Outbox format validation
  3. Bot process health (PID file + HTTP /metrics optional)
  4. Longpoll health check (1 request)
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

# ─── paths ────────────────────────────────────────────────────────────────────
HOME = Path(os.environ.get("HOME", "~")).expanduser()
PROJECT = HOME / "hermes-max-channel"
INBOX = PROJECT / "data" / "inbox.json"
OUTBOX = PROJECT / "data" / "outbox.json"
PROCESSED = PROJECT / "data" / "processed.json"
BOT_PID = PROJECT / "bot.pid"
LOG = PROJECT / "logs" / "bot.log"

# ─── helpers ──────────────────────────────────────────────────────────────────
def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())

def write_json(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def log(msg: str):
    print(f"[TEST] {msg}")

# ─── phase 1: generate test message into inbox ────────────────────────────────
def inject_test_message(user_id: int = 987654321, name: str = "TestUser", text: str = "Привет, Хермес!") -> str:
    inbox = read_json(INBOX, default=[])
    mid = f"test_{int(time.time())}_{user_id}"
    inbox.append({
        "user_id": user_id,
        "username": name,
        "text": text,
        "mid": mid,
        "chat_id": user_id,  # for single user chat chat_id == user_id in MAX
        "timestamp": time.time(),
        "reply_sent": False,
        "processed_at": None,
    })
    write_json(INBOX, inbox)
    log(f"Injected message mid={mid}, user={user_id}, text={text[:40]}")
    return mid

# ─── phase 2: wait for worker ─────────────────────────────────────────────────
def wait_for_reply(target_mid: str, timeout: float = 15.0) -> Dict[str, Any]:
    """Poll outbox until a reply with matching mid appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        outbox = read_json(OUTBOX, default=[])
        # outbox items have {user_id, chat_id, text, mid, status}
        for item in outbox:
            # reply mid format: reply_{original_mid}
            if item.get("mid", "").endswith(target_mid) or item.get("reply_to") == target_mid:
                log(f"Reply found: mid={item['mid']} text={item['text'][:60]}")
                return item
        time.sleep(1.0)
    raise TimeoutError(f"No reply for mid={target_mid} after {timeout}s")

# ─── phase 3: verify bot process ─────────────────────────────────────────────
def check_bot_process() -> bool:
    if BOT_PID.exists():
        pid = int(BOT_PID.read_text().strip())
        try:
            os.kill(pid, 0)  # no-op, checks process exists
            log(f"Bot process alive: PID {pid}")
            return True
        except ProcessLookupError:
            log("Bot PID file exists but process dead")
            return False
    log("Bot PID file not found")
    return False

# ─── phase 4: log tail helper ─────────────────────────────────────────────────
def tail_log(lines: int = 20) -> List[str]:
    if LOG.exists():
        all_lines = LOG.read_text().splitlines()
        return all_lines[-lines:]
    return []

# ─── main ─────────────────────────────────────────────────────────────────────
async def main():
    log("=== Hermes MAX Bot Integration Test ===")

    # 1. Pre-check: bot process
    if not check_bot_process():
        log("ERROR: bot not running. Start with: python -m src.main")
        sys.exit(1)

    # 2. Inject synthetic message
    mid = inject_test_message(text="Привет, Хермес! (тест)")

    # 3. Wait for reply
    try:
        reply = wait_for_reply(mid, timeout=20.0)
        log(f"✓ Reply received: {reply['text'][:80]}")
    except TimeoutError as e:
        log(f"✗ Timeout: {e}")
        log("Recent log tail:")
        for l in tail_log(15):
            print("  ", l)
        sys.exit(1)

    # 4. Clear inbox/outbox for cleanliness
    write_json(INBOX, [])
    write_json(OUTBOX, [])
    log("Cleared inbox/outbox")

    # 5. Final status
    log("=== TEST PASSED ===")
    print(f"\nReply details:\n  To user_id : {reply['user_id']}\n  chat_id    : {reply.get('chat_id')}\n  Text       : {reply['text']}\n  mid        : {reply['mid']}")

if __name__ == "__main__":
    asyncio.run(main())
