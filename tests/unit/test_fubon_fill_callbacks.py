"""Callback-first Fubon fill confirmation tests.

Reuses the FakeSdk fixtures from test_fubon_execution; CallbackFakeSdk adds the
official report hooks (set_on_futopt_filled / set_on_futopt_order /
set_on_order_futopt_changed / set_on_event) so the fill listener activates.
"""

from __future__ import annotations

import pytest

from lux_trader.core.models import OrderSide, StrategyState
from lux_trader.execution import ExecutionOutcomeStatus
from lux_trader.integrations.fubon.execution import FubonFutureExecutionAdapter
from lux_trader.integrations.fubon.fill_listener import FubonFillReportListener

from test_fubon_execution import (
    SYMBOL,
    FakeAccount,
    FakeFutOpt,
    FakeResult,
    FakeSdk,
    active_row,
    adapter_for,
    execution_plan,
    filled_row,
    fubon_leg,
    position_row,
    ts,
)


class CallbackFakeSdk(FakeSdk):
    """FakeSdk with the official report hooks so the fill listener activates."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.handlers: dict[str, object] = {}

    def set_on_futopt_filled(self, handler) -> None:
        self.handlers["filled"] = handler

    def set_on_futopt_order(self, handler) -> None:
        self.handlers["order"] = handler

    def set_on_order_futopt_changed(self, handler) -> None:
        self.handlers["changed"] = handler

    def set_on_event(self, handler) -> None:
        self.handlers["event"] = handler

    def fire_filled(self, **raw) -> None:
        self.handlers["filled"]("200", raw)

    def fire_order_report(self, **raw) -> None:
        self.handlers["order"]("200", raw)

    def fire_event(self, code) -> None:
        self.handlers["event"](code, {})


def fire_fill_during_place(sdk: CallbackFakeSdk, fills: list[dict]) -> None:
    """Deliver fill callbacks BEFORE place_order returns (race case)."""
    original = sdk.futopt.place_order

    def place_and_fill(account, order, unblock):
        result = original(account, order, unblock)
        for fill in fills:
            sdk.fire_filled(**fill)
        return result

    sdk.futopt.place_order = place_and_fill


def test_fill_callback_is_primary_confirmation_even_before_place_returns() -> None:
    sdk = CallbackFakeSdk(
        order_results=[active_row()],
        positions=[],
    )
    fire_fill_during_place(
        sdk,
        [
            {
                "seq_no": "seq-1",
                "filled_no": "f1",
                "filled_lots": 1,
                "filled_price": 101.5,
            }
        ],
    )
    adapter = adapter_for(sdk)

    outcome = adapter.execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.payload["fill_source"] == "filled_callback"
    assert outcome.payload["callback_filled_lot"] == 1.0
    assert len(outcome.payload["fill_events"]) == 1
    assert outcome.fills[0].price == pytest.approx(101.5)
    # callback confirmation means no after-order position fetch is needed
    assert sdk.futopt_accounting.calls == 1
    assert sdk.futopt.get_order_results_calls == []


def test_fill_callbacks_accumulate_partial_fills_to_full() -> None:
    sdk = CallbackFakeSdk(order_results=[active_row()], positions=[])
    fire_fill_during_place(
        sdk,
        [
            {
                "seq_no": "seq-1",
                "filled_no": "f1",
                "filled_lots": 1,
                "filled_price": 100.0,
            },
            {
                "seq_no": "seq-1",
                "filled_no": "f2",
                "filled_lots": 1,
                "filled_price": 102.0,
            },
        ],
    )
    adapter = adapter_for(sdk)
    plan = execution_plan(legs=(fubon_leg(quantity=2.0),))

    outcome = adapter.execute(plan)

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.payload["callback_filled_lot"] == 2.0
    assert outcome.fills[0].quantity == 2.0
    # volume-weighted average of the two fills
    assert outcome.fills[0].price == pytest.approx(101.0)


def test_duplicate_fill_callbacks_are_deduplicated_by_filled_no() -> None:
    sdk = CallbackFakeSdk(order_results=[active_row()], positions=[])
    duplicated = {
        "seq_no": "seq-1",
        "filled_no": "f1",
        "filled_lots": 1,
        "filled_price": 100.0,
    }
    fire_fill_during_place(sdk, [duplicated, dict(duplicated)])
    adapter = adapter_for(sdk)

    outcome = adapter.execute(execution_plan())

    assert outcome.payload["callback_filled_lot"] == 1.0
    assert outcome.status == ExecutionOutcomeStatus.FILLED


def test_order_report_cancel_via_callback_fails_fast() -> None:
    sdk = CallbackFakeSdk(order_results=[active_row()], positions=[])
    original = sdk.futopt.place_order

    def place_and_cancel(account, order, unblock):
        result = original(account, order, unblock)
        sdk.fire_order_report(seq_no="seq-1", status="30")
        return result

    sdk.futopt.place_order = place_and_cancel
    sleeps: list[float] = []
    adapter = FubonFutureExecutionAdapter(
        SYMBOL,
        sdk=sdk,
        account=FakeAccount(),
        clock=ts,
        sleep=sleeps.append,
        max_poll_seconds=5.0,
        poll_interval_seconds=1.0,
    )

    outcome = adapter.execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert sleeps == []  # terminal report ends the wait immediately
    assert outcome.payload["order_reports"]


def test_polled_status_30_cancel_is_terminal_failed_without_waiting() -> None:
    sleeps: list[float] = []
    sdk = FakeSdk(
        order_results=[
            {
                "order_no": "order-1",
                "seq_no": "seq-1",
                "symbol": SYMBOL,
                "expiry_date": "202607",
                "buy_sell": "Buy",
                "lot": 1,
                "status": "30",
                "filled_lot": 0,
            }
        ],
        positions=[],
    )
    adapter = FubonFutureExecutionAdapter(
        SYMBOL,
        sdk=sdk,
        account=FakeAccount(),
        clock=ts,
        sleep=sleeps.append,
        max_poll_seconds=5.0,
        poll_interval_seconds=1.0,
    )

    outcome = adapter.execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert sleeps == []  # official 30=Cancel is final; no timeout burn
    assert len(sdk.futopt.get_order_results_calls) == 1


def test_polled_status_9_timeout_triggers_immediate_requery() -> None:
    class SequencedFutOpt(FakeFutOpt):
        def __init__(self, responses):
            super().__init__()
            self.responses = responses

        def get_order_results(self, account, market_type):
            self.get_order_results_calls.append((account, market_type))
            index = min(
                len(self.get_order_results_calls) - 1, len(self.responses) - 1
            )
            return FakeResult(self.responses[index])

    row_9 = {
        "order_no": "order-1",
        "seq_no": "seq-1",
        "symbol": SYMBOL,
        "expiry_date": "202607",
        "buy_sell": "Buy",
        "lot": 1,
        "status": "9",
        "filled_lot": 0,
    }
    row_50 = dict(row_9, status="50", filled_lot=1)
    sleeps: list[float] = []
    sdk = FakeSdk(position_results=[[], [position_row()]])
    sdk.futopt = SequencedFutOpt([[row_9], [row_50]])
    adapter = FubonFutureExecutionAdapter(
        SYMBOL,
        sdk=sdk,
        account=FakeAccount(),
        clock=ts,
        sleep=sleeps.append,
        max_poll_seconds=2.0,
        poll_interval_seconds=1.0,
    )

    outcome = adapter.execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert len(sdk.futopt.get_order_results_calls) == 2
    assert sleeps == []  # status 9 re-queries without burning the interval


def test_disconnect_event_marks_callback_stream_unreliable() -> None:
    sdk = CallbackFakeSdk(
        order_results=[filled_row(filled_lot=1.0, status="50")],
        position_results=[[], [position_row()]],
    )
    original = sdk.futopt.place_order

    def place_and_disconnect(account, order, unblock):
        result = original(account, order, unblock)
        sdk.fire_event("301")
        return result

    sdk.futopt.place_order = place_and_disconnect
    adapter = adapter_for(sdk)

    outcome = adapter.execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.payload["fill_source"] == "position_delta"
    assert outcome.payload["callback_stream_unreliable"] is True


def test_fill_callback_with_garbage_content_never_raises() -> None:
    sdk = CallbackFakeSdk()
    listener = FubonFillReportListener.attach(sdk)
    # garbage payloads must be swallowed on the SDK thread, never raised
    sdk.handlers["filled"]("200", object())
    sdk.handlers["order"]("200", None)
    sdk.handlers["event"](None, None)
    assert listener.active


def test_listener_attach_is_idempotent_per_sdk() -> None:
    sdk = CallbackFakeSdk()
    first = FubonFillReportListener.attach(sdk)
    second = FubonFillReportListener.attach(sdk)
    assert first is second


def test_sell_side_callback_fill_confirms_exit_leg() -> None:
    sdk = CallbackFakeSdk(order_results=[active_row()], positions=[])
    fire_fill_during_place(
        sdk,
        [
            {
                "seq_no": "seq-1",
                "filled_no": "f1",
                "filled_lots": 1,
                "filled_price": 99.0,
            }
        ],
    )
    adapter = adapter_for(sdk)
    plan = execution_plan(side=OrderSide.SELL)

    outcome = adapter.execute(plan)

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.payload["fill_source"] == "filled_callback"


# --- session reset / listener close --------------------------------------
#
# REGRESSION 2026-08-20: _reset_session's FIRST statement is
# `self.fill_listener.close()`, and the listener had no close(). That made an
# AttributeError out of the ONLY path back from an expired Fubon token -- the
# pre-submit health probe catches a session-invalid error, calls _reset_session
# to re-authenticate, and blew up instead of re-logging-in. Found while an
# overnight idle session was 21 hours past its login.


def test_listener_exposes_close_for_session_reset() -> None:
    assert hasattr(FubonFillReportListener, "close")


def test_reset_session_re_authenticates_instead_of_raising() -> None:
    sdk = CallbackFakeSdk()
    adapter = adapter_for(sdk)
    adapter._ensure_connected()
    assert adapter.fill_listener is not None
    generation_before = adapter.session_generation

    adapter._reset_session("Fubon session event 301")

    assert adapter.fill_listener is None
    assert adapter.sdk is None
    assert adapter.relogin_count == 1

    # ...and the next call has to be able to stand a fresh session back up on
    # top of that, which is the half that actually recovers the order path.
    replacement = CallbackFakeSdk()
    adapter.sdk_factory = lambda: replacement
    adapter._login = lambda _sdk: [FakeAccount()]
    adapter._ensure_connected()

    assert adapter.sdk is replacement
    assert adapter.fill_listener is not None
    assert adapter.session_generation > generation_before
    assert adapter.last_invalid_reason is None


def test_close_wakes_waiters_and_stops_trusting_the_callback_stream() -> None:
    sdk = CallbackFakeSdk()
    listener = FubonFillReportListener.attach(sdk)
    waiter = listener.register_waiter()
    assert listener.active is True

    listener.close()

    assert listener.active is False
    # A logged-out session will never deliver another callback, so anything
    # still waiting must fall back to polling rather than block on it.
    assert listener.stream_unreliable is True
    listener.remove_waiter(waiter)


def test_close_detaches_so_the_next_session_gets_a_new_listener() -> None:
    sdk = CallbackFakeSdk()
    first = FubonFillReportListener.attach(sdk)
    assert FubonFillReportListener.attach(sdk) is first

    first.close()

    second = FubonFillReportListener.attach(sdk)
    assert second is not first


def test_close_is_idempotent_and_never_raises() -> None:
    sdk = CallbackFakeSdk()
    listener = FubonFillReportListener.attach(sdk)
    listener.close()
    listener.close()
