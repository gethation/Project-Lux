from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import IO, Any


class LiveLeaseError(RuntimeError):
    pass


def live_lease_path(store_path: Path) -> Path:
    return Path(f"{store_path}.fubon-live.lock")


class LiveProcessLease:
    """OS-backed advisory lease guarding Project-Lux Fubon login commands."""

    def __init__(self, store_path: Path) -> None:
        self.path = live_lease_path(store_path)
        self._file: IO[str] | None = None

    def acquire(self, *, metadata: dict[str, Any] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            _ensure_lock_byte(handle)
            _lock(handle, nonblocking=True)
        except Exception:
            handle.close()
            raise LiveLeaseError(
                f"Another live-execute process holds {self.path}"
            ) from None
        payload = {
            "pid": os.getpid(),
            "acquired_at": datetime.now().astimezone().isoformat(),
            **(metadata or {}),
        }
        handle.seek(1)
        handle.truncate()
        handle.write(json.dumps(payload, sort_keys=True))
        handle.flush()
        self._file = handle

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "LiveProcessLease":
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


def assert_live_lease_available(store_path: Path) -> None:
    path = live_lease_path(store_path)
    if not path.exists():
        return
    handle = path.open("a+", encoding="utf-8")
    try:
        _ensure_lock_byte(handle)
        try:
            _lock(handle, nonblocking=True)
        except Exception:
            raise LiveLeaseError(
                "A live-execute process is active; direct Fubon login is blocked. "
                "Use live-status instead."
            ) from None
        _unlock(handle)
    finally:
        handle.close()


def _ensure_lock_byte(handle: IO[str]) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)


if os.name == "nt":
    import msvcrt

    def _lock(handle: IO[str], *, nonblocking: bool) -> None:
        handle.seek(0)
        mode = msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK
        msvcrt.locking(handle.fileno(), mode, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str], *, nonblocking: bool) -> None:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        fcntl.flock(handle.fileno(), flags)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "LiveLeaseError",
    "LiveProcessLease",
    "assert_live_lease_available",
    "live_lease_path",
]
