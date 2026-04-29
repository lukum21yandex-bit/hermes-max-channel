#!/usr/bin/env python3
"""
Hermes MAX Bot — manual test helper

Usage:
    python test_manual.py inject "Привет!"      # inject message
    python test_manual.py check                 # wait for reply + show logs
    python test_manual.py full                  # inject + wait (one-shot)
    python test_manual.py logs                  # tail bot logs
    python test_manual.py status                # bot health check
"""
import asyncio, json, os, sys, time
from pathlib import Path

HOME = Path.home()
PROJECT = HOME / "hermes-max-channel"
INBOX = PROJECT / "data" / "inbox.json"
OUTBOX = PROJECT / "data" / "outbox.json"
LOG = PROJECT / "logs" / "bot.log"
PID_FILE = PROJECT / "bot.pid"


# ─── helpers ──────────────────────────────────────────────────────────────────
def log(msg: str):
    print(f"[test] {msg}")


def status():
    print(" Hermes MAX Bot — status")
    print("-" * 40)
    if PID_FILE.exists():
        pid = PID_FILE.read_text().strip()
        try:
            os.kill(int(pid), 0)
            print(f"  Bot process : RUNNING (PID {pid})")
        except ProcessLookupError:
            print(f"  Bot process : DEAD (stale PID {pid})")
    else:
        print("  Bot process : NOT RUNNING (no bot.pid)")

    ib = INBOX.exists() and json.loads(INBOX.read_text())
    ob = OUTBOX.exists() and json.loads(OUTBOX.read_text())
    print(f"  Inbox size  : {len(ib) if ib else 0} messages")
    print(f"  Outbox size : {len(ob) if ob else 0} messages")

    if LOG.exists():
        print(f"  Log file    : {LOG}")
        print("  Last 5 lines:")
        for l in LOG.read_text().splitlines()[-5:]:
            print(f"    {l}")


def inject(text: str, user_id: int = 999000111, name: str = "ManualTest"):
    inbox = json.loads(INBOX.read_text()) if INBOX.exists() else []
    mid = f"manual_{int(time.time())}_{user_id}"
    inbox.append({
        "user_id": user_id,
        "username": name,
        "text": text,
        "mid": mid,
        "chat_id": user_id,
        "timestamp": time.time(),
        "reply_sent": False,
    })
    INBOX.write_text(json.dumps(inbox, ensure_ascii=False, indent=2))
    log(f"Injected message → mid={mid}, text={text[:50]}")
    print(f"Message ID: {mid}")


def wait_for_reply(target_mid: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    log(f"Waiting up to {timeout}s for reply to {target_mid}...")
    while time.time() < deadline:
        ob = json.loads(OUTBOX.read_text()) if OUTBOX.exists() else []
        for item in ob:
            if item.get("mid", "").endswith(target_mid):
                print(f"\n✓ Reply received:")
                print(f"  From bot  : {item['text'][:120]}")
                print(f"  To user   : {item['user_id']}")
                print(f"  mid       : {item['mid']}")
                return item
        time.sleep(1.0)
    print(f"\n✗ No reply after {timeout}s")
    return None


def tail_log(lines: int = 15):
    if LOG.exists():
        for l in LOG.read_text().splitlines()[-lines:]:
            print(" ", l)
    else:
        print("No log file")


def full_cycle(text: str):
    """Inject + wait in one command."""
    inject(text)
    # extract mid from the last inbox entry
    inbox = json.loads(INBOX.read_text())
    mid = inbox[-1]["mid"]
    wait_for_reply(mid)


# ─── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "status":
        status()
    elif cmd == "logs":
        tail_log()
    elif cmd == "inject":
        if len(sys.argv) < 3:
            print("Usage: test_manual.py inject <text>")
            sys.exit(1)
        inject(sys.argv[2])
    elif cmd == "check":
        if len(sys.argv) < 3:
            print("Usage: test_manual.py check <mid>")
            sys.exit(1)
        wait_for_reply(sys.argv[2])
    elif cmd == "full":
        if len(sys.argv) < 3:
            print("Usage: test_manual.py full <text>")
            sys.exit(1)
        full_cycle(sys.argv[2])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
