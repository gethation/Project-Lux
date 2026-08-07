"""Durable crash records for the live commands.

On 2026-08-07 the live-execute loop died mid-exit of an order-id collision.
Everything that knew why was ephemeral: ``reporter.error()`` carries only
``str(exc)``, the live-execute handler re-raises RuntimeError as ``SystemExit``
(which prints the message and drops the stack), and the terminal scrollback was
gone by the time anyone looked. The single surviving copy was an ntfy push --
one line, no traceback, and only because that topic happened to be enabled.

This writes the full stack to a file under ``log/`` so the next incident has a
record that outlives the terminal. It is deliberately best-effort: a crash
logger that raises would mask the crash it exists to explain.

Nothing here imports a brokerage package, so it stays importable on a machine
that has none -- the same rule ``integrations/venues.py`` follows.
"""

from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

LOG_DIR_NAME = "log"
FILE_PREFIX = "lux_error"


def crash_log_path(when: datetime | None = None, log_dir: Path | None = None) -> Path:
    stamp = (when or datetime.now().astimezone()).strftime("%Y%m%d")
    directory = log_dir if log_dir is not None else Path(LOG_DIR_NAME)
    return directory / f"{FILE_PREFIX}.{stamp}.log"


def record_crash(
    exc: BaseException,
    *,
    context: str,
    log_dir: Path | None = None,
    when: datetime | None = None,
) -> Path | None:
    """Append a timestamped traceback. Returns the path, or None if it could not.

    Never raises: the caller is already handling an exception and must be free
    to re-raise it unchanged.
    """
    moment = when or datetime.now().astimezone()
    try:
        path = crash_log_path(moment, log_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        stack = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        block = (
            f"\n{'=' * 78}\n"
            f"{moment.isoformat(timespec='seconds')}  context={context}\n"
            f"{type(exc).__name__}: {exc}\n"
            f"{'-' * 78}\n"
            f"{stack}"
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(block)
            handle.flush()
        return path
    except Exception:  # noqa: BLE001 - must never mask the original failure
        return None
