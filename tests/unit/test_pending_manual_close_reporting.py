from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

from lux_trader.config import NtfyConfig
from lux_trader.core.models import StrategyAction
from lux_trader.ntfy import NtfyLiveReporter
from lux_trader.terminal_ui import LiveTerminalReporter, NullLiveReporter


class FakePublisher:
    def __init__(self) -> None:
        self.messages = []

    def publish(self, message) -> bool:
        self.messages.append(message)
        return True

    def close(self) -> None:
        return None


def ntfy_config() -> NtfyConfig:
    return NtfyConfig(
        enabled=True,
        server_url="https://ntfy.example",
        status_topic="status",
        trades_topic="trades",
        errors_topic="errors",
    )


def spread() -> SimpleNamespace:
    return SimpleNamespace(
        mid_spread=1.0,
        mid_zscore=0.0,
        short_spread=1.0,
        short_zscore=0.0,
        long_spread=1.0,
        long_zscore=0.0,
    )


def pending_state() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(value="flat"),
        position_direction=None,
        pnl_status="pending",
    )


def test_terminal_marks_realized_pnl_pending() -> None:
    stream = io.StringIO()
    reporter = LiveTerminalReporter(stream, color=False)

    reporter.bar(
        datetime(2026, 7, 22, tzinfo=timezone.utc),
        spread(),
        pending_state(),
        StrategyAction.NONE,
        "",
        0.0,
        0.0,
    )

    assert "realized_pnl=PENDING" in stream.getvalue()


def test_ntfy_warns_once_that_realized_pnl_excludes_manual_close() -> None:
    publisher = FakePublisher()
    reporter = NtfyLiveReporter(
        NullLiveReporter(),
        ntfy_config(),
        mode="live-execute",
        publisher=publisher,
    )
    timestamp = datetime(2026, 7, 22, tzinfo=timezone.utc)

    for _ in range(2):
        reporter.bar(
            timestamp,
            spread(),
            pending_state(),
            StrategyAction.NONE,
            "",
            0.0,
            0.0,
        )

    pending_messages = [
        message
        for message in publisher.messages
        if "realized PnL pending" in message.title
    ]
    assert len(pending_messages) == 1
    assert "realized_pnl excludes" in pending_messages[0].message
