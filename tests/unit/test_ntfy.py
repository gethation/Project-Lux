from __future__ import annotations

import io
import threading
from argparse import Namespace
from datetime import datetime
from types import SimpleNamespace

import pytest

from lux_trader.config import NtfyConfig, load_ntfy_config
from lux_trader.cli import commands_live
from lux_trader.core.models import BrokerName, Direction, Fill, OrderSide, StrategyAction
from lux_trader.execution.intent import ExecutionPlanType
from lux_trader.execution.outcome import ExecutionOutcomeStatus
from lux_trader.ntfy import (
    ALERT_PRIORITY,
    STATUS_PRIORITY,
    NtfyLiveReporter,
    NtfyMessage,
    NtfyPublisher,
    notify_operational_error,
)
from lux_trader.terminal_ui import NullLiveReporter


def ts() -> datetime:
    return datetime.fromisoformat("2026-07-14T09:12:00+08:00")


def config() -> NtfyConfig:
    return NtfyConfig(
        enabled=True,
        server_url="https://ntfy.sh",
        status_topic="status-abc12",
        trades_topic="trades-def34",
        errors_topic="errors-ghi56",
        request_timeout_seconds=3.0,
    )


class FakeBaseReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return record


class FakePublisher:
    def __init__(self) -> None:
        self.messages: list[NtfyMessage] = []
        self.closed = False

    def publish(self, message: NtfyMessage) -> bool:
        self.messages.append(message)
        return True

    def close(self) -> None:
        self.closed = True


def reporter(*, mode: str = "live-execute") -> tuple[NtfyLiveReporter, FakePublisher]:
    publisher = FakePublisher()
    return (
        NtfyLiveReporter(
            FakeBaseReporter(),
            config(),
            mode=mode,
            publisher=publisher,
        ),
        publisher,
    )


def test_load_ntfy_config_validates_enabled_topics_and_normalizes_url() -> None:
    parsed = load_ntfy_config(
        {
            "enabled": True,
            "server_url": "https://ntfy.sh/",
            "status_topic": "/status-a/",
            "trades_topic": "trades-b",
            "errors_topic": "errors-c",
            "request_timeout_seconds": 1.5,
        }
    )
    assert parsed.server_url == "https://ntfy.sh"
    assert parsed.status_topic == "status-a"
    assert parsed.request_timeout_seconds == pytest.approx(1.5)

    with pytest.raises(ValueError, match="trades_topic"):
        load_ntfy_config(
            {
                "enabled": True,
                "status_topic": "status-a",
                "errors_topic": "errors-c",
            }
        )


def test_bar_publishes_low_priority_structured_account_status() -> None:
    ntfy, publisher = reporter()
    snapshot = SimpleNamespace(mid_spread=10.83, short_zscore=-2.12, long_zscore=-1.86)
    state = SimpleNamespace(
        state=SimpleNamespace(value="open"),
        position_direction=Direction.LONG_TSM_SHORT_QFF,
    )
    account = SimpleNamespace(
        combined_upnl_twd=12345.0,
        binance_ratio=1.42,
        fubon_ratio=1.38,
        stale=False,
    )

    ntfy.bar(ts(), snapshot, state, StrategyAction.NONE, "", 0.0, 0.0, account)

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message.topic == "status-abc12"
    assert message.priority == STATUS_PRIORITY
    assert "mode=LIVE" in message.message
    assert "mid=10.83 short_z=-2.12 long_z=-1.86 state=LONG" in message.message
    assert "pnl=12,345 margin(bina=142%,fubon=138%)" in message.message


def test_dry_run_filled_execution_is_clearly_labeled_and_lists_both_fills() -> None:
    ntfy, publisher = reporter(mode="live-dry-run")
    plan = SimpleNamespace(
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_TSM_LONG_QFF,
    )
    fills = (
        Fill(
            fill_id="f1",
            order_id="o1",
            broker=BrokerName.BINANCE_TSM,
            symbol="TSM/USDT:USDT",
            side=OrderSide.SELL,
            quantity=2.5,
            price=105.25,
            fee_twd=10.0,
            timestamp=ts(),
            row_index=1,
        ),
        Fill(
            fill_id="f2",
            order_id="o2",
            broker=BrokerName.FUBON_QFF,
            symbol="QFFQ6",
            side=OrderSide.BUY,
            quantity=1.0,
            price=842.0,
            fee_twd=88.0,
            timestamp=ts(),
            row_index=1,
        ),
    )
    outcome = SimpleNamespace(
        status=ExecutionOutcomeStatus.FILLED,
        fills=fills,
        message="simulated execution filled",
    )
    result = SimpleNamespace(reason="dry_run_filled")

    ntfy.execution(ts(), plan, outcome, result)

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message.topic == "trades-def34"
    assert message.priority == ALERT_PRIORITY
    assert "[DRY-RUN]" in message.title
    assert "mode=DRY-RUN" in message.message
    assert "BINANCE_TSM TSM/USDT:USDT sell qty=2.5 price=105.25" in message.message
    assert "FUBON_QFF QFFQ6 buy qty=1 price=842" in message.message


def test_partial_execution_publishes_fill_and_error_notifications() -> None:
    ntfy, publisher = reporter()
    plan = SimpleNamespace(
        plan_type=ExecutionPlanType.EXIT,
        direction=Direction.LONG_TSM_SHORT_QFF,
    )
    fill = Fill(
        fill_id="f1",
        order_id="o1",
        broker=BrokerName.BINANCE_TSM,
        symbol="TSM/USDT:USDT",
        side=OrderSide.SELL,
        quantity=1.0,
        price=100.0,
        fee_twd=5.0,
        timestamp=ts(),
        row_index=1,
    )
    outcome = SimpleNamespace(
        status=ExecutionOutcomeStatus.PARTIAL_FILL,
        fills=(fill,),
        message="second leg failed",
    )

    ntfy.execution(ts(), plan, outcome, SimpleNamespace(reason="live_exit_partial"))

    assert [message.topic for message in publisher.messages] == [
        "trades-def34",
        "errors-ghi56",
    ]
    assert "exit_execution_partial_fill" in publisher.messages[1].message


def test_runtime_and_operational_errors_use_high_priority_error_topic() -> None:
    ntfy, publisher = reporter()

    ntfy.error(ts(), "RuntimeError: broker unavailable")
    notify_operational_error(ntfy, ts(), "post_trade_reconciliation_mismatch", "error")

    assert len(publisher.messages) == 2
    assert all(message.topic == "errors-ghi56" for message in publisher.messages)
    assert all(message.priority == ALERT_PRIORITY for message in publisher.messages)


class FakeResponse:
    def raise_for_status(self) -> None:
        return


def test_background_publisher_prioritizes_alerts_and_replaces_old_status() -> None:
    release = threading.Event()
    started = threading.Event()
    payloads: list[dict] = []

    def transport(url, *, json, timeout):
        payloads.append(json)
        if len(payloads) == 1:
            started.set()
            assert release.wait(2.0)
        return FakeResponse()

    publisher = NtfyPublisher(config(), transport=transport, queue_size=2)
    try:
        publisher.publish(NtfyMessage("status", "one", "one", 2))
        assert started.wait(1.0)
        publisher.publish(NtfyMessage("status", "old", "old", 2))
        publisher.publish(NtfyMessage("status", "new", "new", 2))
        publisher.publish(NtfyMessage("errors", "alert", "alert", 4))
        release.set()
        assert publisher.drain(2.0)
    finally:
        release.set()
        publisher.close()

    assert [payload["title"] for payload in payloads] == ["one", "alert", "new"]


def test_publish_failure_is_logged_without_raising_to_caller() -> None:
    errors = io.StringIO()

    def transport(url, *, json, timeout):
        raise OSError("offline")

    publisher = NtfyPublisher(config(), transport=transport, error_stream=errors)
    try:
        assert publisher.publish(NtfyMessage("errors", "alert", "body", 4))
        assert publisher.drain(1.0)
    finally:
        publisher.close()

    assert "ntfy publish failed; trading continues" in errors.getvalue()


def test_quiet_ui_still_wraps_reporter_with_ntfy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeNtfyReporter:
        def __init__(self, base, ntfy_config, *, mode):
            captured.update(base=base, config=ntfy_config, mode=mode)

    monkeypatch.setattr(commands_live, "NtfyLiveReporter", FakeNtfyReporter)
    app_config = SimpleNamespace(ntfy=config())

    wrapped = commands_live.build_live_reporter(
        Namespace(quiet_ui=True, no_color=False, ui="compact"),
        app_config,
        mode="live-execute",
    )

    assert isinstance(wrapped, FakeNtfyReporter)
    assert isinstance(captured["base"], NullLiveReporter)
    assert captured["mode"] == "live-execute"
