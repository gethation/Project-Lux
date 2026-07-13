"""Fubon fill/order report listener: callback-first fill confirmation.

Registers the SDK-global futures report callbacks once per SDK session
(``set_on_futopt_filled`` / ``set_on_futopt_order`` /
``set_on_order_futopt_changed`` / ``set_on_event`` — see the official
Event/Notification Callback guide) and turns them into per-order fill
accumulation that ``FubonFutureExecutionAdapter`` can wait on.

Hard rules learned from the official docs and M6:
- Callbacks run on the SDK's report thread: never raise inside one (it can
  break the report connection), so every callback body is wrapped and errors
  are recorded instead.
- Registration is global per SDK object — one dispatcher, not one per order.
- A fill callback can arrive BEFORE ``place_order`` returns the seq_no, so
  fills that match no active waiter are buffered (bounded) and re-claimed once
  the waiter learns its keys.
- Event codes 301 (Pong Missing) / 302 (Manually disconnected) mark the
  callback stream unreliable; waiters then rely on the polling backup.

If the SDK object lacks the ``set_on_*`` hooks (e.g. test fakes), the listener
stays inactive and the adapter falls back to pure polling unchanged.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ...core.contracts import row_to_dict
from .parsing import fubon_first_float, fubon_first_text, safe_jsonable


UNRELIABLE_EVENT_CODES = {"301", "302"}
UNMATCHED_FILL_BUFFER_SIZE = 64

_ATTACHED_LISTENERS: "dict[int, FubonFillReportListener]" = {}
_ATTACH_LOCK = threading.Lock()


def fill_keys(raw: dict[str, Any]) -> tuple[str, ...]:
    keys = []
    for names in (("seq_no", "seqNo"), ("order_no", "orderNo", "ord_no")):
        value = fubon_first_text(raw, *names)
        if value:
            keys.append(str(value).strip())
    return tuple(keys)


def fill_lots(raw: dict[str, Any]) -> float:
    value = fubon_first_float(
        raw,
        "filled_lots",
        "filled_lot",
        "filledLots",
        "filledLot",
        "match_lot",
        "matchLot",
        "deal_lot",
        "dealLot",
        "lot",
        "lots",
    )
    return float(value or 0.0)


def fill_price(raw: dict[str, Any]) -> float | None:
    return fubon_first_float(
        raw,
        "filled_price",
        "filledPrice",
        "match_price",
        "matchPrice",
        "deal_price",
        "dealPrice",
        "price",
    )


@dataclass
class FillWaiter:
    """Per-order fill accumulator; keys learned after place_order returns."""

    listener: "FubonFillReportListener"
    keys: set[str] = field(default_factory=set)
    filled_lots: float = 0.0
    fill_events: list[dict[str, Any]] = field(default_factory=list)
    order_reports: list[dict[str, Any]] = field(default_factory=list)
    terminal_status: str | None = None
    _seen_fill_ids: set[str] = field(default_factory=set)

    def set_keys(self, *keys: str | None) -> None:
        with self.listener.condition:
            for key in keys:
                if key:
                    self.keys.add(str(key).strip())
            self.listener._reclaim_buffered_fills_locked(self)
            self.listener.condition.notify_all()

    def matches(self, raw: dict[str, Any]) -> bool:
        if not self.keys:
            return False
        return any(key in self.keys for key in fill_keys(raw))

    def absorb_fill_locked(self, raw: dict[str, Any]) -> None:
        # De-duplicate on the fill serial when present (a report can be
        # re-delivered after reconnect).
        fill_id = fubon_first_text(raw, "filled_no", "filledNo") or ""
        dedupe_key = fill_id or repr(sorted(raw.items()))
        if dedupe_key in self._seen_fill_ids:
            return
        self._seen_fill_ids.add(dedupe_key)
        self.filled_lots += fill_lots(raw)
        self.fill_events.append(safe_jsonable(raw) or {})

    def average_fill_price(self) -> float | None:
        total_lots = 0.0
        total_money = 0.0
        for event in self.fill_events:
            lots = fill_lots(event)
            price = fill_price(event)
            if lots > 0 and price is not None:
                total_lots += lots
                total_money += lots * price
        if total_lots <= 0:
            return None
        return total_money / total_lots

    def wait(self, timeout: float) -> None:
        with self.listener.condition:
            self.listener.condition.wait(timeout=timeout)

    def close(self) -> None:
        self.listener.remove_waiter(self)


class FubonFillReportListener:
    """One dispatcher per SDK session; inactive when the SDK has no hooks."""

    def __init__(self, sdk: Any) -> None:
        self.condition = threading.Condition()
        self.active = False
        self.stream_unreliable = False
        self.callback_errors: list[dict[str, Any]] = []
        self._waiters: list[FillWaiter] = []
        self._unmatched_fills: deque[dict[str, Any]] = deque(
            maxlen=UNMATCHED_FILL_BUFFER_SIZE
        )
        self._unmatched_reports: deque[dict[str, Any]] = deque(
            maxlen=UNMATCHED_FILL_BUFFER_SIZE
        )
        self._register(sdk)

    @classmethod
    def attach(cls, sdk: Any) -> "FubonFillReportListener":
        with _ATTACH_LOCK:
            existing = getattr(sdk, "_lux_fill_listener", None)
            if isinstance(existing, cls):
                return existing
            # Registry fallback only ever holds SDK objects that rejected
            # attribute assignment (real bindings, process lifetime), so the
            # id-reuse hazard does not apply to test fakes.
            registered = _ATTACHED_LISTENERS.get(id(sdk))
            if registered is not None:
                return registered
            listener = cls(sdk)
            try:
                setattr(sdk, "_lux_fill_listener", listener)
            except Exception:
                _ATTACHED_LISTENERS[id(sdk)] = listener
            return listener

    # ------------------------------------------------------------------
    def register_waiter(self) -> FillWaiter:
        waiter = FillWaiter(listener=self)
        with self.condition:
            self._waiters.append(waiter)
        return waiter

    def remove_waiter(self, waiter: FillWaiter) -> None:
        with self.condition:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    # ------------------------------------------------------------------
    def _register(self, sdk: Any) -> None:
        hooks = (
            ("set_on_futopt_filled", self._on_filled),
            ("set_on_futopt_order", self._on_order),
            ("set_on_order_futopt_changed", self._on_order_changed),
            ("set_on_event", self._on_event),
        )
        registered = 0
        for name, handler in hooks:
            setter = getattr(sdk, name, None)
            if callable(setter):
                try:
                    setter(handler)
                    registered += 1
                except Exception as exc:
                    self.callback_errors.append(
                        {
                            "stage": name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        }
                    )
        # Fill callbacks are the primary confirmation channel; order/event
        # hooks are supporting. Active only when the fill hook registered.
        self.active = registered > 0 and callable(
            getattr(sdk, "set_on_futopt_filled", None)
        )

    # -- SDK-thread callbacks (never raise) ----------------------------
    def _on_filled(self, code: Any, content: Any) -> None:
        try:
            raw = row_to_dict(content)
            with self.condition:
                matched = False
                for waiter in self._waiters:
                    if waiter.matches(raw):
                        waiter.absorb_fill_locked(raw)
                        matched = True
                if not matched:
                    self._unmatched_fills.append(raw)
                self.condition.notify_all()
        except Exception as exc:  # pragma: no cover - defensive
            self._record_callback_error("on_filled", exc)

    def _on_order(self, code: Any, content: Any) -> None:
        self._absorb_order_report("on_order", content)

    def _on_order_changed(self, code: Any, content: Any) -> None:
        self._absorb_order_report("on_order_changed", content)

    def _absorb_order_report(self, stage: str, content: Any) -> None:
        try:
            raw = row_to_dict(content)
            with self.condition:
                matched = False
                for waiter in self._waiters:
                    if waiter.matches(raw):
                        apply_order_report_locked(waiter, raw)
                        matched = True
                if not matched:
                    self._unmatched_reports.append(raw)
                self.condition.notify_all()
        except Exception as exc:  # pragma: no cover - defensive
            self._record_callback_error(stage, exc)

    def _on_event(self, code: Any, content: Any) -> None:
        try:
            if str(code).strip() in UNRELIABLE_EVENT_CODES:
                with self.condition:
                    self.stream_unreliable = True
                    self.condition.notify_all()
        except Exception as exc:  # pragma: no cover - defensive
            self._record_callback_error("on_event", exc)

    def _record_callback_error(self, stage: str, exc: Exception) -> None:
        try:
            self.callback_errors.append(
                {
                    "stage": stage,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _reclaim_buffered_fills_locked(self, waiter: FillWaiter) -> None:
        remaining: deque[dict[str, Any]] = deque(maxlen=self._unmatched_fills.maxlen)
        for raw in self._unmatched_fills:
            if waiter.matches(raw):
                waiter.absorb_fill_locked(raw)
            else:
                remaining.append(raw)
        self._unmatched_fills = remaining
        remaining_reports: deque[dict[str, Any]] = deque(
            maxlen=self._unmatched_reports.maxlen
        )
        for raw in self._unmatched_reports:
            if waiter.matches(raw):
                apply_order_report_locked(waiter, raw)
            else:
                remaining_reports.append(raw)
        self._unmatched_reports = remaining_reports


def apply_order_report_locked(waiter: FillWaiter, raw: dict[str, Any]) -> None:
    waiter.order_reports.append(safe_jsonable(raw) or {})
    status = fubon_first_text(raw, "status", "order_status", "orderStatus")
    if status:
        waiter.terminal_status = str(status).strip()
