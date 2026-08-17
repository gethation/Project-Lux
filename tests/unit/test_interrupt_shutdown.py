"""Ctrl+C ends a live session; it is not a crash.

Covers the two halves separately: the parent turns KeyboardInterrupt into a
quiet exit, and the spawned workers detach from the console's Ctrl+C so they do
not each print a traceback of their own.
"""

from __future__ import annotations

import signal

import pytest

from lux_trader.cli import dispatch
from lux_trader.integrations.subprocess_transport import ignore_parent_interrupt


def test_ctrl_c_exits_quietly_with_the_shell_convention(capsys, monkeypatch) -> None:
    def interrupted(_argv):
        raise KeyboardInterrupt

    monkeypatch.setattr(dispatch, "main", interrupted)

    code = dispatch.run([])

    assert code == 130  # 128 + SIGINT
    captured = capsys.readouterr()
    assert "Interrupted" in captured.err
    assert "Traceback" not in captured.err
    assert "KeyboardInterrupt" not in captured.err


def test_a_normal_exit_code_still_passes_through(monkeypatch) -> None:
    """The wrapper must not swallow or rewrite a real result."""
    monkeypatch.setattr(dispatch, "main", lambda _argv: 3)
    assert dispatch.run([]) == 3


def test_a_real_failure_is_not_disguised_as_an_interrupt(monkeypatch) -> None:
    """Only KeyboardInterrupt is special. Anything else must still propagate,
    or the crash log and the non-zero exit both stop happening."""

    def boom(_argv):
        raise RuntimeError("gate closed")

    monkeypatch.setattr(dispatch, "main", boom)

    with pytest.raises(RuntimeError, match="gate closed"):
        dispatch.run([])


def test_workers_detach_from_the_console_interrupt() -> None:
    """Windows sends CTRL_C_EVENT to every process on the console, so without
    this each spawned worker raises inside its own recv() and prints its own
    traceback -- four of them for one deliberate Ctrl+C."""
    previous = signal.getsignal(signal.SIGINT)
    try:
        ignore_parent_interrupt()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        # Idempotent: workers are rebuilt on timeout, so this runs repeatedly.
        ignore_parent_interrupt()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous)
