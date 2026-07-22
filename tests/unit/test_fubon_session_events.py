from __future__ import annotations

from typing import Any

from lux_trader.integrations.fubon.fill_listener import FubonFillReportListener


class EventSdk:
    def __init__(self) -> None:
        self.on_event = None

    def set_on_futopt_filled(self, _handler: Any) -> None:
        return None

    def set_on_futopt_order(self, _handler: Any) -> None:
        return None

    def set_on_order_futopt_changed(self, _handler: Any) -> None:
        return None

    def set_on_event(self, handler: Any) -> None:
        self.on_event = handler


def test_fill_listener_forwards_sdk_session_event_to_adapter_observer() -> None:
    sdk = EventSdk()
    observed: list[tuple[Any, Any]] = []

    listener = FubonFillReportListener.attach(
        sdk,
        event_observer=lambda code, content: observed.append((code, content)),
    )
    assert sdk.on_event is not None

    sdk.on_event(304, "credential invalid")

    assert observed == [(304, "credential invalid")]
