from __future__ import annotations

import io
import sqlite3
import threading
from argparse import Namespace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import requests

import lux_trader.ntfy as ntfy_module
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
    NtfyStatusCommandSubscriber,
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


def test_bar_does_not_publish_periodic_status() -> None:
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

    assert publisher.messages == []


def test_bar_strategy_error_still_publishes_alert() -> None:
    ntfy, publisher = reporter()
    snapshot = SimpleNamespace(mid_spread=10.83, short_zscore=-2.12, long_zscore=-1.86)

    ntfy.bar(
        ts(),
        snapshot,
        SimpleNamespace(state=SimpleNamespace(value="flat")),
        StrategyAction.ERROR,
        "indicator_failed",
        0.0,
        0.0,
    )

    assert len(publisher.messages) == 1
    assert publisher.messages[0].topic == "errors-ghi56"
    assert "strategy_error" in publisher.messages[0].message


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
    assert "trade_pnl_twd" not in message.message


def test_filled_exit_notification_includes_authoritative_trade_pnl() -> None:
    ntfy, publisher = reporter()
    plan = SimpleNamespace(
        plan_type=ExecutionPlanType.EXIT,
        direction=Direction.LONG_TSM_SHORT_QFF,
    )
    fill = SimpleNamespace(
        broker=SimpleNamespace(value="fubon_qff"),
        symbol="QFFQ6",
        side=SimpleNamespace(value="buy"),
        quantity=1.0,
        price=842.0,
        fee_twd=88.0,
    )
    outcome = SimpleNamespace(
        status=ExecutionOutcomeStatus.FILLED,
        fills=(fill,),
        message="exit filled",
    )
    result = SimpleNamespace(
        reason="live_filled",
        trade={
            "net_pnl_twd": 12_345.4,
            "gross_pnl_twd": 12_500.4,
            "total_fee_twd": 155.0,
        },
    )

    ntfy.execution(ts(), plan, outcome, result)

    assert len(publisher.messages) == 1
    assert (
        "trade_pnl_twd net=12,345 gross=12,500 fees=155"
        in publisher.messages[0].message
    )


def test_filled_exit_notification_marks_missing_trade_pnl_unavailable() -> None:
    ntfy, publisher = reporter()
    plan = SimpleNamespace(
        plan_type=ExecutionPlanType.EXIT,
        direction=Direction.LONG_TSM_SHORT_QFF,
    )
    outcome = SimpleNamespace(
        status=ExecutionOutcomeStatus.FILLED,
        fills=(
            SimpleNamespace(
                broker=SimpleNamespace(value="fubon_qff"),
                symbol="QFFQ6",
                side=SimpleNamespace(value="buy"),
                quantity=1.0,
                price=842.0,
                fee_twd=88.0,
            ),
        ),
        message="exit filled",
    )

    ntfy.execution(ts(), plan, outcome, SimpleNamespace(reason="live_filled"))

    assert publisher.messages[0].message.endswith("trade_pnl_twd unavailable")


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
    assert "trade_pnl_twd" not in publisher.messages[0].message
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


def seed_status_bars(path, count: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE bars (
                row_index INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                spread REAL,
                short_zscore REAL,
                long_zscore REAL,
                state TEXT NOT NULL,
                position TEXT NOT NULL,
                unrealized_pnl REAL NOT NULL
            )
            """
        )
        start = ts()
        for index in range(count):
            state = "open" if index == count - 1 else "flat"
            position = "long_tsm_short_qff" if state == "open" else "flat"
            connection.execute(
                """
                INSERT INTO bars (
                    row_index, timestamp, spread, short_zscore, long_zscore,
                    state, position, unrealized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index,
                    (start + timedelta(minutes=index)).isoformat(),
                    10.0 + index / 10.0,
                    1.0 + index / 10.0,
                    1.5 + index / 10.0,
                    state,
                    position,
                    index * 1000.0,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def command_subscriber(path, publisher, *, error_stream=None):
    return NtfyStatusCommandSubscriber(
        config(),
        store_path=path,
        mode_label="LIVE",
        publisher=publisher,
        transport=lambda *args, **kwargs: None,
        error_stream=error_stream,
    )


def test_status_command_replies_with_latest_ten_committed_bars(tmp_path) -> None:
    store_path = tmp_path / "live.sqlite3"
    seed_status_bars(store_path, 12)
    publisher = FakePublisher()
    subscriber = command_subscriber(store_path, publisher)

    assert not subscriber.handle_event(
        {"event": "message", "id": "ignore", "message": "hello"}
    )
    assert subscriber.handle_event(
        {"event": "message", "id": "command-1", "message": " STATUS "}
    )
    assert not subscriber.handle_event(
        {"event": "message", "id": "command-1", "message": "status"}
    )
    assert not subscriber.handle_event(
        {
            "event": "message",
            "id": "reply-1",
            "message": "07-14 09:11\nmid=11.10 zS=2.10 zL=2.60",
        }
    )

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message.title == "[LIVE] Latest 10 BARs"
    blocks = message.message.split("\n\n")
    assert len(blocks) == 10
    assert blocks[0].startswith("07-14 09:14\nmid=10.20 zS=1.20 zL=1.70")
    assert blocks[-1].endswith("state=LONG PnL=11,000 TWD")


def test_status_command_reports_available_count_and_empty_store(tmp_path) -> None:
    partial_path = tmp_path / "partial.sqlite3"
    seed_status_bars(partial_path, 2)
    partial_publisher = FakePublisher()
    partial = command_subscriber(partial_path, partial_publisher)

    assert partial.handle_event(
        {"event": "message", "id": "partial", "message": "status"}
    )
    assert partial_publisher.messages[0].title == "[LIVE] Latest 2 BARs"

    empty_path = tmp_path / "empty.sqlite3"
    seed_status_bars(empty_path, 0)
    empty_publisher = FakePublisher()
    empty = command_subscriber(empty_path, empty_publisher)

    assert empty.handle_event(
        {"event": "message", "id": "empty", "message": "status"}
    )
    assert empty_publisher.messages[0].title == "[LIVE] Latest 0 BARs"
    assert empty_publisher.messages[0].message == "No committed BAR data"


def test_status_command_labels_non_trading_session(tmp_path) -> None:
    store_path = tmp_path / "live.sqlite3"
    seed_status_bars(store_path, 10)
    publisher = FakePublisher()
    subscriber = command_subscriber(store_path, publisher)
    subscriber.set_trading_session(False)

    assert subscriber.handle_event(
        {"event": "message", "id": "non-trading", "message": "status"}
    )

    assert (
        publisher.messages[0].title
        == "[LIVE] non-trading session Latest 10 BARs"
    )


def test_trading_session_transition_publishes_once_to_trades_topic() -> None:
    ntfy, publisher = reporter()

    ntfy.live_non_trading(
        ts(),
        ts() + timedelta(hours=1),
        "scheduled_break",
    )
    ntfy.trading_session(ts() + timedelta(hours=1))
    ntfy.trading_session(ts() + timedelta(hours=1, seconds=1))

    assert len(publisher.messages) == 1
    message = publisher.messages[0]
    assert message.topic == "trades-def34"
    assert message.title == "[LIVE] trading session started"
    assert message.priority == ALERT_PRIORITY
    assert "session=trading" in message.message


def test_status_query_failure_is_non_fatal_and_returns_unavailable(tmp_path) -> None:
    publisher = FakePublisher()
    errors = io.StringIO()
    subscriber = command_subscriber(
        tmp_path / "missing.sqlite3",
        publisher,
        error_stream=errors,
    )

    assert subscriber.handle_event(
        {"event": "message", "id": "missing", "message": "status"}
    )

    assert publisher.messages[0].title == "[LIVE] Status unavailable"
    assert "command subscriber failed; trading continues" in errors.getvalue()


def test_cached_bootstrap_message_is_not_replayed(tmp_path) -> None:
    store_path = tmp_path / "live.sqlite3"
    seed_status_bars(store_path, 1)
    publisher = FakePublisher()

    class CachedResponse:
        def raise_for_status(self):
            return None

        def iter_lines(self):
            return iter(
                [
                    b'{"event":"open","id":"open"}',
                    b'{"event":"message","id":"old-status","message":"status"}',
                ]
            )

        def close(self):
            return None

    subscriber = NtfyStatusCommandSubscriber(
        config(),
        store_path=store_path,
        mode_label="LIVE",
        publisher=publisher,
        transport=lambda *args, **kwargs: CachedResponse(),
    )

    subscriber._bootstrap_cursor()

    assert not subscriber.handle_event(
        {"event": "message", "id": "old-status", "message": "status"}
    )
    assert publisher.messages == []


def test_command_subscriber_network_failure_is_logged_without_raising(tmp_path) -> None:
    errors = io.StringIO()

    def offline(*args, **kwargs):
        raise OSError("offline")

    subscriber = NtfyStatusCommandSubscriber(
        config(),
        store_path=tmp_path / "live.sqlite3",
        mode_label="LIVE",
        publisher=FakePublisher(),
        transport=offline,
        error_stream=errors,
    )

    subscriber._bootstrap_cursor()

    assert "command subscriber failed; trading continues" in errors.getvalue()


def test_command_subscriber_reconnects_after_429(monkeypatch, tmp_path) -> None:
    store_path = tmp_path / "live.sqlite3"
    seed_status_bars(store_path, 1)
    publisher = FakePublisher()
    stream_requested = threading.Event()
    call_count = 0

    class StreamResponse:
        def __init__(self, lines=(), *, rate_limited=False):
            self.lines = tuple(lines)
            self.rate_limited = rate_limited

        def raise_for_status(self):
            if self.rate_limited:
                response = SimpleNamespace(status_code=429, headers={})
                raise requests.HTTPError("429 Too Many Requests", response=response)

        def iter_lines(self):
            return iter(self.lines)

        def close(self):
            return None

    def transport(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:  # cached-message bootstrap
            return StreamResponse()
        if call_count == 2:
            return StreamResponse(rate_limited=True)
        stream_requested.set()
        return StreamResponse(
            [b'{"event":"message","id":"fresh","message":"status"}']
        )

    monkeypatch.setattr(ntfy_module, "SUBSCRIBER_BACKOFF_SECONDS", (0.01,))
    subscriber = NtfyStatusCommandSubscriber(
        config(),
        store_path=store_path,
        mode_label="LIVE",
        publisher=publisher,
        transport=transport,
        error_stream=io.StringIO(),
    )

    subscriber.start()
    try:
        assert stream_requested.wait(1.0)
        deadline = datetime.now().timestamp() + 1.0
        while not publisher.messages and datetime.now().timestamp() < deadline:
            threading.Event().wait(0.01)
    finally:
        subscriber.close()

    assert call_count >= 3
    assert publisher.messages[0].title == "[LIVE] Latest 1 BARs"


def test_reporter_stops_command_subscriber_before_publisher() -> None:
    events: list[str] = []

    class OrderedPublisher(FakePublisher):
        def close(self):
            events.append("publisher")
            super().close()

    class OrderedSubscriber:
        def start(self):
            events.append("start")

        def close(self):
            events.append("subscriber")

    publisher = OrderedPublisher()
    reporter = NtfyLiveReporter(
        FakeBaseReporter(),
        config(),
        mode="live-execute",
        publisher=publisher,
        command_subscriber=OrderedSubscriber(),
    )

    reporter.finish()

    assert events == ["start", "subscriber", "publisher"]


def test_quiet_ui_still_wraps_reporter_with_ntfy(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class FakeNtfyReporter:
        def __init__(self, base, ntfy_config, *, mode, store_path):
            captured.update(
                base=base,
                config=ntfy_config,
                mode=mode,
                store_path=store_path,
            )

    monkeypatch.setattr(commands_live, "NtfyLiveReporter", FakeNtfyReporter)
    app_config = SimpleNamespace(
        ntfy=config(),
        store_path=tmp_path / "live.sqlite3",
    )

    wrapped = commands_live.build_live_reporter(
        Namespace(quiet_ui=True, no_color=False, ui="compact"),
        app_config,
        mode="live-execute",
    )

    assert isinstance(wrapped, FakeNtfyReporter)
    assert isinstance(captured["base"], NullLiveReporter)
    assert captured["mode"] == "live-execute"
    assert captured["store_path"] == app_config.store_path
