from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TextIO

import requests

from .config import NtfyConfig
from .core.time import ensure_taipei
from .execution.outcome import ExecutionOutcomeStatus
from .terminal_ui import (
    format_float,
    format_money,
)
from .trade_pnl import format_trade_pnl_values, trade_pnl_from_execution


STATUS_PRIORITY = 2
ALERT_PRIORITY = 4
DEFAULT_QUEUE_SIZE = 128
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS = 2.0
FAILURE_LOG_INTERVAL_SECONDS = 60.0
STATUS_BAR_COUNT = 10
STATUS_COMMAND = "status"
SUBSCRIBER_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
SUBSCRIBER_READ_TIMEOUT_SECONDS = 75.0
SEEN_MESSAGE_ID_LIMIT = 256


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


@dataclass(frozen=True)
class NtfyStatusBar:
    timestamp: datetime
    mid_spread: float | None
    short_zscore: float | None
    long_zscore: float | None
    state: str
    position: str
    unrealized_pnl: float


def load_recent_status_bars(
    store_path: Path,
    *,
    limit: int = STATUS_BAR_COUNT,
) -> tuple[NtfyStatusBar, ...]:
    """Load committed BARs without touching the live writer connection."""

    path = Path(store_path).resolve()
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """
            SELECT timestamp, spread, short_zscore, long_zscore,
                   state, position, unrealized_pnl
            FROM bars
            ORDER BY row_index DESC
            LIMIT ?
            """,
            (max(int(limit), 1),),
        ).fetchall()
    finally:
        connection.close()

    return tuple(
        NtfyStatusBar(
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            mid_spread=float(row["spread"]) if row["spread"] is not None else None,
            short_zscore=(
                float(row["short_zscore"])
                if row["short_zscore"] is not None
                else None
            ),
            long_zscore=(
                float(row["long_zscore"])
                if row["long_zscore"] is not None
                else None
            ),
            state=str(row["state"]),
            position=str(row["position"]),
            unrealized_pnl=float(row["unrealized_pnl"]),
        )
        for row in reversed(rows)
    )


def format_status_state(state: str, position: str) -> str:
    state_text = str(state).upper()
    if state_text != "OPEN":
        return state_text
    position_text = str(position).lower()
    if position_text == "long_us_short_tw":
        return "LONG"
    if position_text == "short_us_long_tw":
        return "SHORT"
    return state_text


def format_status_bars(bars: tuple[NtfyStatusBar, ...]) -> str:
    blocks = []
    for bar in bars:
        blocks.append(
            "\n".join(
                (
                    bar.timestamp.strftime("%m-%d %H:%M"),
                    f"mid={format_float(bar.mid_spread, digits=2)} "
                    f"zS={format_float(bar.short_zscore, digits=2)} "
                    f"zL={format_float(bar.long_zscore, digits=2)}",
                    f"state={format_status_state(bar.state, bar.position)} "
                    f"PnL={format_money(bar.unrealized_pnl)} TWD",
                )
            )
        )
    return "\n\n".join(blocks)


class NtfyStatusCommandSubscriber:
    """Best-effort ntfy command listener isolated from the live loop."""

    def __init__(
        self,
        config: NtfyConfig,
        *,
        store_path: Path,
        mode_label: str,
        publisher: Any,
        transport: Callable[..., Any] | None = None,
        error_stream: TextIO | None = None,
    ) -> None:
        self.config = config
        self.store_path = Path(store_path)
        self.mode_label = mode_label
        self.publisher = publisher
        self._session = requests.Session() if transport is None else None
        self._transport = transport or self._session.get
        self._error_stream = error_stream or sys.stderr
        self._last_failure_log = 0.0
        self._stop = threading.Event()
        self._response_lock = threading.Lock()
        self._active_response: Any = None
        self._last_message_id: str | None = None
        self._seen_message_ids: deque[str] = deque()
        self._seen_message_id_set: set[str] = set()
        self._is_trading_session: bool | None = None
        self._pnl_pending_notified = False
        self._thread: threading.Thread | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.config.server_url}/{self.config.status_topic}/json"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="project-lux-ntfy-command",
            daemon=True,
        )
        self._thread.start()

    def close(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        self._stop.set()
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(max(float(timeout), 0.0))
        if self._session is not None:
            self._session.close()

    def handle_event(self, event: dict[str, Any]) -> bool:
        if str(event.get("event", "")) != "message":
            return False
        message_id = str(event.get("id", "")).strip()
        if message_id:
            if message_id in self._seen_message_id_set:
                return False
            self._remember_message_id(message_id)
            self._last_message_id = message_id
        if str(event.get("message", "")).strip().casefold() != STATUS_COMMAND:
            return False
        self._publish_recent_bars()
        return True

    def set_trading_session(self, is_trading: bool) -> None:
        self._is_trading_session = bool(is_trading)

    def _remember_message_id(self, message_id: str) -> None:
        self._seen_message_ids.append(message_id)
        self._seen_message_id_set.add(message_id)
        while len(self._seen_message_ids) > SEEN_MESSAGE_ID_LIMIT:
            expired = self._seen_message_ids.popleft()
            self._seen_message_id_set.discard(expired)

    def _publish_recent_bars(self) -> None:
        try:
            bars = load_recent_status_bars(
                self.store_path,
                limit=STATUS_BAR_COUNT,
            )
        except Exception as exc:
            self._log_failure(exc)
            self.publisher.publish(
                NtfyMessage(
                    topic=self.config.status_topic,
                    title=f"[{self.mode_label}] Status unavailable",
                    message="BAR data temporarily unavailable",
                    priority=STATUS_PRIORITY,
                    tags=("warning",),
                )
            )
            return

        session_label = (
            "non-trading session " if self._is_trading_session is False else ""
        )
        if not bars:
            title = f"[{self.mode_label}] {session_label}Latest 0 BARs"
            body = "No committed BAR data"
        else:
            title = (
                f"[{self.mode_label}] {session_label}Latest {len(bars)} BARs"
            )
            body = format_status_bars(bars)
        self.publisher.publish(
            NtfyMessage(
                topic=self.config.status_topic,
                title=title,
                message=body,
                priority=STATUS_PRIORITY,
                tags=("bar_chart",),
            )
        )

    def _run(self) -> None:
        backoff_index = 0
        try:
            self._bootstrap_cursor()
            while not self._stop.is_set():
                try:
                    params = {
                        "since": self._last_message_id or str(int(time.time()))
                    }
                    response = self._transport(
                        self.endpoint,
                        params=params,
                        stream=True,
                        timeout=(
                            self.config.request_timeout_seconds,
                            SUBSCRIBER_READ_TIMEOUT_SECONDS,
                        ),
                    )
                    with self._response_lock:
                        self._active_response = response
                    response.raise_for_status()
                    backoff_index = 0
                    for raw_line in response.iter_lines():
                        if self._stop.is_set():
                            break
                        if not raw_line:
                            continue
                        try:
                            event = json.loads(raw_line)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(event, dict):
                            self.handle_event(event)
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    self._log_failure(exc)
                    delay = SUBSCRIBER_BACKOFF_SECONDS[
                        min(backoff_index, len(SUBSCRIBER_BACKOFF_SECONDS) - 1)
                    ]
                    backoff_index += 1
                    self._stop.wait(delay)
                finally:
                    with self._response_lock:
                        response = self._active_response
                        self._active_response = None
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
        finally:
            if self._session is not None:
                self._session.close()

    def _bootstrap_cursor(self) -> None:
        """Start after the latest cached event so old commands are not replayed."""

        try:
            response = self._transport(
                self.endpoint,
                params={"poll": "1", "since": "latest"},
                stream=True,
                timeout=self.config.request_timeout_seconds,
            )
            with self._response_lock:
                self._active_response = response
            response.raise_for_status()
            for raw_line in response.iter_lines():
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict) or event.get("event") != "message":
                    continue
                message_id = str(event.get("id", "")).strip()
                if message_id:
                    self._remember_message_id(message_id)
                    self._last_message_id = message_id
        except Exception as exc:
            if not self._stop.is_set():
                self._log_failure(exc)
        finally:
            with self._response_lock:
                response = self._active_response
                self._active_response = None
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _log_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        if now - self._last_failure_log < FAILURE_LOG_INTERVAL_SECONDS:
            return
        self._last_failure_log = now
        self._error_stream.write(
            "WARN ntfy command subscriber failed; trading continues: "
            f"{type(exc).__name__}: {exc}\n"
        )
        self._error_stream.flush()


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
        store_path: Path | None = None,
        command_subscriber: NtfyStatusCommandSubscriber | None = None,
    ) -> None:
        self.base = base_reporter
        self.config = config
        self.mode = mode
        self.mode_label = "LIVE" if mode == "live-execute" else "DRY-RUN"
        self.publisher = publisher or NtfyPublisher(config)
        self._is_trading_session: bool | None = None
        self._pnl_pending_notified = False
        self.command_subscriber = command_subscriber
        if self.command_subscriber is None and store_path is not None:
            self.command_subscriber = NtfyStatusCommandSubscriber(
                config,
                store_path=store_path,
                mode_label=self.mode_label,
                publisher=self.publisher,
            )
        if self.command_subscriber is not None:
            self.command_subscriber.start()

    def live(self, *args: Any, **kwargs: Any) -> None:
        self._set_trading_session(True)
        self.base.live(*args, **kwargs)

    def live_non_trading(self, *args: Any, **kwargs: Any) -> None:
        self._set_trading_session(False)
        self.base.live_non_trading(*args, **kwargs)

    def _set_trading_session(self, is_trading: bool) -> None:
        self._is_trading_session = bool(is_trading)
        setter = getattr(self.command_subscriber, "set_trading_session", None)
        if callable(setter):
            setter(is_trading)

    @ntfy_best_effort
    def trading_session(self, timestamp: Any) -> None:
        previous = self._is_trading_session
        self._set_trading_session(True)
        if previous is not False:
            return
        self.publisher.publish(
            NtfyMessage(
                topic=self.config.trades_topic,
                title=f"[{self.mode_label}] trading session started",
                message=(
                    f"{format_timestamp(timestamp)} mode={self.mode_label} "
                    "session=trading"
                ),
                priority=ALERT_PRIORITY,
                tags=("bell",),
            )
        )

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
        base_execution = getattr(self.base, "execution", None)
        if callable(base_execution):
            try:
                base_execution(timestamp, plan, outcome, result)
            except Exception as exc:
                log_failure = getattr(self.publisher, "_log_failure", None)
                if callable(log_failure):
                    log_failure(exc)
                else:
                    try:
                        sys.stderr.write(
                            "WARN terminal execution reporter failed; "
                            f"trading continues: {type(exc).__name__}: {exc}\n"
                        )
                    except Exception:
                        pass
        fills = tuple(getattr(outcome, "fills", ()) or ())
        plan_type = str(getattr(getattr(plan, "plan_type", None), "value", "trade"))
        status = str(getattr(getattr(outcome, "status", None), "value", "unknown"))
        if fills:
            fill_lines = [format_fill(fill) for fill in fills]
            pnl_summary = trade_pnl_from_execution(plan, outcome, result)
            if pnl_summary is not None:
                pnl_values = format_trade_pnl_values(pnl_summary)
                fill_lines.append(
                    f"trade_pnl_twd {pnl_values}"
                    if pnl_values is not None
                    else "trade_pnl_twd unavailable"
                )
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
            try:
                if self.command_subscriber is not None:
                    self.command_subscriber.close()
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
