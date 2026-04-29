"""
File storage utilities with atomic writes and cross-process locking.

Best practices implemented:
  - Atomic writes: write to temp file, fsync, rename (POSIX atomic)
  - Advisory file locks via fcntl.flock() for concurrent access
  - Lock timeout configurable, avoid deadlocks
  - Safe reads with lock (optional, for consistency)
"""
import fcntl
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, TypeVar

import logging

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ─── Lock helpers ────────────────────────────────────────────────────────────

class FileLock:
    """Cross-process file lock using fcntl.flock (advisory, non-blocking)."""

    def __init__(self, lock_path: Path, timeout: float = 10.0):
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self._lock_fd: Optional[int] = None
        self._locked = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def acquire(self):
        start = time.monotonic()
        while True:
            try:
                # Open (or create) lock file
                self._lock_fd = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_RDWR,
                    0o644,
                )
                # Try non-blocking lock
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._locked = True
                return
            except (OSError, BlockingIOError):
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                if time.monotonic() - start > self.timeout:
                    raise TimeoutError(f"Could not acquire lock on {self.lock_path} after {self.timeout}s")
                time.sleep(0.1)

    def release(self):
        if self._locked and self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._lock_fd)
            except Exception:
                pass
        self._locked = False
        self._lock_fd = None


def with_lock(lock_path: Path, timeout: float = 10.0):
    """Decorator / context manager for file-locked sections."""
    return FileLock(lock_path, timeout=timeout)


# ─── Atomic JSON I/O ─────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: str) -> None:
    """
    Write data to path atomically:
      1. Write to .<path>.tmp.<pid> in same directory
      2. fsync() to disk
      3. rename() (atomic on POSIX)
      4. cleanup stale .tmp files on error
    """
    tmp_name = f"{path.name}.tmp.{os.getpid()}"
    tmp_path = path.parent / tmp_name

    try:
        # Write temp file
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

        # Atomic replace
        os.replace(tmp_path, path)
    except Exception as e:
        # Cleanup temp on failure
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def atomic_read_json(path: Path, default=None) -> Any:
    """Read JSON atomically (no lock — for cases where consistency not critical)."""
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        logger.error("JSON decode error reading %s: %s", path, e)
        return default


def atomic_write_json(path: Path, data: Any, **kwargs) -> None:
    """
    Write data as JSON atomically with fsync.
    Extra kwargs passed to json.dump (indent, ensure_ascii...).
    """
    import json
    text = json.dumps(data, ensure_ascii=kwargs.pop("ensure_ascii", False), **kwargs)
    _atomic_write(path, text)


def locked_read_json(data_path: Path, lock_path: Path, default=None) -> Any:
    """
    Read JSON with exclusive lock (writer gets EX lock, reader gets SH lock).
    For critical reads where you must see a consistent snapshot.
    """
    with FileLock(lock_path):
        return atomic_read_json(data_path, default=default)


def locked_write_json(data_path: Path, lock_path: Path, data: Any, **kwargs) -> None:
    """Write JSON with exclusive lock."""
    with FileLock(lock_path):
        atomic_write_json(data_path, data, **kwargs)


# ─── Lock-free atomic append (for inbox where multiple writers possible) ──────

def atomic_append_json_array(
    array_path: Path,
    new_item: Any,
    lock_path: Optional[Path] = None,
    id_field: Optional[str] = None,
) -> None:
    """
    Atomically append item to JSON array file.

    If lock_path provided — acquires exclusive lock before read-modify-write.
    If no lock — relies on atomic rename, but concurrent writers may lose updates
    (acceptable for low-contention cases like our inbox where only one writer
    typically exists — the bot).
    """
    import json

    def _do_append():
        if array_path.exists():
            arr = json.loads(array_path.read_text(encoding="utf-8"))
            if not isinstance(arr, list):
                arr = []
        else:
            arr = []
        arr.append(new_item)
        atomic_write_json(array_path, arr)

    if lock_path:
        with FileLock(lock_path):
            _do_append()
    else:
        _do_append()


# ─── Idempotency helpers ─────────────────────────────────────────────────────

def find_item_by_id(items: list, id_field: str, id_value: Any) -> Optional[Dict]:
    """Find first item where item[id_field] == id_value."""
    for item in items:
        if item.get(id_field) == id_value:
            return item
    return None


def mark_item_by_id(
    items: list,
    id_field: str,
    id_value: Any,
    updates: Dict[str, Any],
) -> bool:
    """
    Find item by id and patch it with updates. Returns True if found.
    Does NOT write to disk — caller should persist.
    """
    for item in items:
        if item.get(id_field) == id_value:
            item.update(updates)
            return True
    return False


# ─── Cleanup / archival ───────────────────────────────────────────────────────
