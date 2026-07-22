from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, TextIO

import requests

from .config import NtfyConfig
from .core.time import ensure_taipei
from .execution.outcome import ExecutionOutcomeStatus
from .terminal_ui import (
    account_margin_text,
    account_pnl_text,
    format_float,
    state_value,
)


STATUS_PRIORITY = 2
ALERT_PRIORITY = 4
DEFAULT_QUEUE_SIZE = 128
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0
FAILURE_LOG_INTERVAL_SECONDS = 60.0


def ntfy_best_effort(method: Callable[..., Any]) -> Callable[..., None]:
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> None:
        try:
            method(self, *args, **kwargs)
        except Exception as exc:
            log_failure = getattr(self.publisher, "_log_failure", None)
            if callable(log_failure):
                log_failure(exc)
            else:  # test/custom publishers need no logging contract
                try:
                    sys.stderr.write(
                        "WARN ntfy reporter failed; trading continues: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                except Exception:
                    pass

    return wrapped


@dataclass(frozen=True)
class NtfyMessage:
    topic: str
    title: str
    message: str
    priority: int
    tags: tuple[str, ...] = ()

    def to_jsonable(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic": self.topic,
            "title": self.title,
            "message": self.message,
            "priority": self.priority,
        }
        if self.tags:
            payload["tags"] = list(self.tags)
        return payload


class NtfyPublisher:
    """Best-effort, non-blocking ntfy publisher for the live trading loop."""

    def __init__(
        self,
        config: NtfyConfig,
        *,
        transport: Callable[..., Any] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        error_stream: TextIO | None = None,
    ) -> None:
        self.config = config
        self._session = requests.Session() if transport is None else None
        self._transport = transport or self._session.post
        self._queue_size = max(int(queue_size), 1)
        self._items: deque[NtfyMessage] = deque()
        self._condition = threading.Condition()
        self._closing = False
        self._in_flight = False
        self._error_stream = error_stream or sys.stderr
        self._last_failure_log = 0.0
        self._worker = threading.Thread(
            target=self._run,
            name="project-lux-ntfy",
            daemon=True,
        )
        self._worker.start()

    def publish(self, message: NtfyMessage) -> bool:
        with self._condition:
            if self._closing:
                return False
            if len(self._items) >= self._queue_size:
                minimum_priority = min(item.priority for item in self._items)
                if message.priority > minimum_priority or (
                    message.priority <= STATUS_PRIORITY
                    and minimum_priority <= STATUS_PRIORITY
                ):
                    for index, item in enumerate(self._items):
                        if item.priority == minimum_priority:
                            del self._items[index]
                            break
                else:
                    return False
            self._items.append(message)
            self._condition.notify()
            return True

    def close(
        self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
    ) -> None:
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._worker.join(max(float(timeout), 0.0))

    def drain(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        with self._condition:
            while self._items or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._items and not self._closing:
                        self._condition.wait()
                    if not self._items and self._closing:
                        return
                    max_priority = max(item.priority for item in self._items)
                    index = next(
                        index
                        for index, item in enumerate(self._items)
                        if item.priority == max_priority
                    )
                    message = self._items[index]
                    del self._items[index]
                    self._in_flight = True
                try:
                    response = self._transport(
                        self.config.server_url,
                        json=message.to_jsonable(),
                        timeout=self.config.request_timeout_seconds,
                    )
                    response.raise_for_status()
                except Exception as exc:  # notification failure must not stop trading
                    self._log_failure(exc)
                finally:
                    with self._condition:
                        self._in_flight = False
                        self._condition.notify_all()
        finally:
            if self._session is not None:
                self._session.close()

    def _log_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_failure_log < FAILURE_LOG_INTERVAL_SECONDS:
            return
        self._last_failure_log = now
        self._error_stream.write(
            "WARN ntfy publish failed; trading continues: "
            f"{type(exc).__name__}: {exc}\n"
        )
        self._error_stream.flush()


class NtfyLiveReporter:
    """Reporter decorator that preserves the selected UI and adds ntfy."""

    def __init__(
        self,
        base_reporter: Any,
        config: NtfyConfig,
        *,
        mode: str,
        publisher: NtfyPublisher | None = None,
    ) -> None:
        self.base = base_reporter
        self.config = config
        self.mode = mode
        self.mode_label = "LIVE" if mode == "live-execute" else "DRY-RUN"
        self.publisher = publisher or NtfyPublisher(config)
        self._pnl_pending_notified = False

    def live(self, *args: Any, **kwargs: Any) -> None:
        self.base.live(*args, **kwargs)

    def live_non_trading(self, *args: Any, **kwargs: Any) -> None:
        self.base.live_non_trading(*args, **kwargs)

    def bar(
        self,
        timestamp: Any,
        spread_snapshot: Any,
        strategy_state: Any,
        action: Any,
        reason: str,
        unrealized_pnl: float,
        equity: float,
        account_display: Any = None,
    ) -> None:
        self.base.bar(
            timestamp,
            spread_snapshot,
            strategy_state,
            action,
            reason,
            unrealized_pnl,
            equity,
            account_display=account_display,
        )
        self._publish_status(
            timestamp,
            spread_snapshot,
            strategy_state,
            action,
            reason,
            account_display,
        )
        if (
            getattr(strategy_state, "pnl_status", "complete") != "complete"
            and not self._pnl_pending_notified
        ):
            self._pnl_pending_notified = True
            self.publisher.publish(
                NtfyMessage(
                    topic=self.config.status_topic,
                    title=f"[{self.mode_label}] realized PnL pending",
                    message=(
                        f"{format_timestamp(timestamp)} realized_pnl excludes "
                        "externally manual-closed trade"
                    ),
                    priority=ALERT_PRIORITY,
                    tags=("warning",),
                )
            )

    @ntfy_best_effort
    def _publish_status(
        self,
        timestamp: Any,
        spread_snapshot: Any,
        strategy_state: Any,
        action: Any,
        reason: str,
        account_display: Any,
    ) -> None:
        stamp = format_timestamp(timestamp)
        self.publisher.publish(
            NtfyMessage(
                topic=self.config.status_topic,
                title=f"[{self.mode_label}] Project Lux status",
                message=(
                    f"{stamp} mode={self.mode_label} "
                    f"mid={format_float(spread_snapshot.mid_spread, digits=2)} "
                    f"short_z={format_float(spread_snapshot.short_zscore, digits=2)} "
                    f"long_z={format_float(spread_snapshot.long_zscore, digits=2)} "
                    f"state={state_value(strategy_state)} "
                    f"pnl={account_pnl_text(account_display)} "
                    f"{account_margin_text(account_display)}"
                ),
                priority=STATUS_PRIORITY,
                tags=("bar_chart",),
            )
        )
        if str(getattr(action, "value", action)) == "error":
            self.notify_error(timestamp, "strategy_error", reason)

    def warn(self, *args: Any, **kwargs: Any) -> None:
        self.base.warn(*args, **kwargs)

    def event(self, *args: Any, **kwargs: Any) -> None:
        self.base.event(*args, **kwargs)

    def error(self, timestamp: Any, message: str) -> None:
        self.base.error(timestamp, message)
        self.notify_error(timestamp, "runtime_error", message)

    @ntfy_best_effort
    def execution(
        self,
        timestamp: Any,
        plan: Any,
        outcome: Any,
        result: Any,
    ) -> None:
        fills = tuple(getattr(outcome, "fills", ()) or ())
        plan_type = str(getattr(getattr(plan, "plan_type", None), "value", "trade"))
        status = str(getattr(getattr(outcome, "status", None), "value", "unknown"))
        if fills:
            fill_lines = [format_fill(fill) for fill in fills]
            self.publisher.publish(
                NtfyMessage(
                    topic=self.config.trades_topic,
                    title=f"[{self.mode_label}] {plan_type.upper()} {status.upper()}",
                    message="\n".join(
                        [
                            f"{format_timestamp(timestamp)} mode={self.mode_label}",
                            f"direction={getattr(getattr(plan, 'direction', None), 'value', '-')}",
                            f"reason={getattr(result, 'reason', '')}",
                            *fill_lines,
                        ]
                    ),
                    priority=ALERT_PRIORITY,
                    tags=("moneybag",),
                )
            )
        if getattr(outcome, "status", None) != ExecutionOutcomeStatus.FILLED:
            self.notify_error(
                timestamp,
                f"{plan_type}_execution_{status}",
                str(getattr(outcome, "message", "")),
            )
        elif not fills:
            self.notify_error(
                timestamp,
                f"{plan_type}_execution_missing_fills",
                "execution outcome is FILLED but contains no fills",
            )

    @ntfy_best_effort
    def notify_error(self, timestamp: Any, code: str, detail: str = "") -> None:
        body = f"{format_timestamp(timestamp)} mode={self.mode_label} code={code}"
        if detail:
            body = f"{body}\n{detail}"
        self.publisher.publish(
            NtfyMessage(
                topic=self.config.errors_topic,
                title=f"[{self.mode_label}] Project Lux error",
                message=body,
                priority=ALERT_PRIORITY,
                tags=("warning",),
            )
        )

    def finish(self) -> None:
        try:
            self.base.finish()
        finally:
            self.publisher.close()


def notify_operational_error(
    reporter: Any,
    timestamp: Any,
    code: str,
    detail: str = "",
) -> None:
    notify = getattr(reporter, "notify_error", None)
    if callable(notify):
        notify(timestamp, code, detail)


def notify_execution(
    reporter: Any,
    timestamp: Any,
    plan: Any,
    outcome: Any,
    result: Any,
) -> None:
    notify = getattr(reporter, "execution", None)
    if callable(notify):
        notify(timestamp, plan, outcome, result)


def format_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return ensure_taipei(value).isoformat(timespec="seconds")
    return str(value)


def format_fill(fill: Any) -> str:
    broker = getattr(getattr(fill, "broker", None), "value", "-")
    side = getattr(getattr(fill, "side", None), "value", "-")
    return (
        f"{broker} {getattr(fill, 'symbol', '-')} {side} "
        f"qty={format_quantity(getattr(fill, 'quantity', 0.0))} "
        f"price={format_quantity(getattr(fill, 'price', 0.0))} "
        f"fee_twd={format_quantity(getattr(fill, 'fee_twd', 0.0))}"
    )


def format_quantity(value: Any) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")
