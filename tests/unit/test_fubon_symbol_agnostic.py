"""One Fubon adapter, many contracts.

A Fubon account permits a single SDK session, and QFF and CCF share one account,
so the execution adapter cannot bind a symbol at construction the way it used to
-- one instance has to serve both pairs' futures.

What must NOT be lost in that move is the wrong-instrument guard. It used to
compare the plan's leg against the symbol handed to the constructor: an
independent reference. Deriving identity from the leg alone would check the plan
against itself, so the caller now supplies ``expected_symbol`` from the pair's
own resolved front month, and the adapter refuses anything else.
"""

from __future__ import annotations

import pytest

from lux_trader.execution import ExecutionOutcomeStatus
from lux_trader.integrations.fubon.execution import FubonFutureExecutionAdapter

from tests.unit.test_fubon_execution import (
    SYMBOL,
    FakeAccount,
    FakeSdk,
    adapter_for,
    execution_plan,
    filled_row,
    position_row,
)


OTHER_SYMBOL = "CCFH6"


def test_the_wrong_instrument_guard_survives_as_a_per_call_check() -> None:
    """A plan for a different contract is rejected, not sent."""
    fake_sdk = FakeSdk(order_results=[filled_row()])

    outcome = adapter_for(fake_sdk).execute(
        execution_plan(),
        expected_symbol=OTHER_SYMBOL,
    )

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert SYMBOL in outcome.message and OTHER_SYMBOL in outcome.message
    assert fake_sdk.futopt.place_calls == []


def test_an_empty_expected_symbol_is_refused_rather_than_defaulted() -> None:
    """Nothing may fall back to 'whatever the plan says'."""
    fake_sdk = FakeSdk(order_results=[filled_row()])

    outcome = adapter_for(fake_sdk).execute(execution_plan(), expected_symbol="  ")

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert "expected_symbol" in outcome.message
    assert fake_sdk.futopt.place_calls == []


def test_a_matching_symbol_places_the_order() -> None:
    fake_sdk = FakeSdk(
        order_results=[filled_row()],
        position_results=[[], [position_row()]],
    )

    outcome = adapter_for(fake_sdk).execute(
        execution_plan(),
        expected_symbol=SYMBOL,
    )

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert len(fake_sdk.futopt.place_calls) == 1


def test_identity_is_derived_per_symbol_not_per_adapter() -> None:
    """The same instance answers for both pairs' contracts."""
    adapter = FubonFutureExecutionAdapter(sdk=FakeSdk(), account=FakeAccount())

    qff = adapter.identity_for(SYMBOL)
    ccf = adapter.identity_for(OTHER_SYMBOL)

    assert qff.requested_symbol == SYMBOL
    assert ccf.requested_symbol == OTHER_SYMBOL
    assert qff is not ccf
    # Cached, so the hot path does not re-parse on every broker row.
    assert adapter.identity_for(SYMBOL) is qff


def test_the_identity_cache_is_keyed_by_date_too() -> None:
    """Contract-month resolution reads the reference date, so a process running
    across midnight must not keep yesterday's answer."""
    from datetime import datetime, timedelta

    from lux_trader.core.time import TAIPEI_TZ

    now = datetime(2026, 7, 26, 23, 59, tzinfo=TAIPEI_TZ)
    clock = {"value": now}
    adapter = FubonFutureExecutionAdapter(
        sdk=FakeSdk(),
        account=FakeAccount(),
        clock=lambda: clock["value"],
    )

    before = adapter.identity_for(SYMBOL)
    clock["value"] = now + timedelta(minutes=2)  # past midnight
    after = adapter.identity_for(SYMBOL)

    assert before is not after


def test_position_and_order_queries_name_their_contract() -> None:
    """Without a symbol these would have to guess, and a shared account holds
    more than one pair's positions."""
    adapter = FubonFutureExecutionAdapter(sdk=FakeSdk(), account=FakeAccount())

    for call in (
        lambda: adapter.fetch_open_orders(),
        lambda: adapter.fetch_position_quantity(),
        lambda: adapter.fetch_order_records(),
        lambda: adapter.preflight(),
    ):
        with pytest.raises(TypeError):
            call()
