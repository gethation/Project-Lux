"""The pre-order position check.

The scenario it exists for: a stock-loan recall buys in the UMC short mid
session without asking. Our state still says OPEN. Later the strategy sends
"buy back the short" -- and with nothing to buy back, that order OPENS A NEW
LONG, leaving long CCF and long UMC in a strategy whose whole premise is being
market neutral.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import BrokerName, Direction, OrderSide
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.execution.position_guard import (
    adapter_position_reader,
    signed_close_quantity,
    verify_plan_against_broker,
)


TS = datetime.fromisoformat("2026-07-29T22:00:00+08:00")


def leg(broker: BrokerName, symbol: str, side: OrderSide, quantity: float) -> ExecutionLeg:
    return ExecutionLeg(
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=100.0,
        timestamp=TS,
        row_index=1,
    )


def plan(plan_type: ExecutionPlanType, *legs: ExecutionLeg) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id="PLAN-1",
        plan_type=plan_type,
        direction=Direction.SHORT_UMC_LONG_CCF,
        timestamp=TS,
        row_index=1,
        legs=tuple(legs),
        reason="test",
    )


def exit_plan_closing_a_short_umc() -> PairExecutionPlan:
    # Closing SHORT_UMC_LONG_CCF: buy back the UMC short, sell the CCF long.
    return plan(
        ExecutionPlanType.EXIT,
        leg(BrokerName.FUBON_CCF, "CCFG6", OrderSide.SELL, 3),
        leg(BrokerName.IBKR_UMC, "UMC", OrderSide.BUY, 1200),
    )


def reader(positions: dict[BrokerName, float | None]):
    return lambda broker, _symbol: positions[broker]


def test_exit_passes_when_the_broker_holds_what_is_being_closed() -> None:
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader({BrokerName.FUBON_CCF: 3.0, BrokerName.IBKR_UMC: -1200.0}),
    )

    assert result.passed
    assert result.failures() == ()


def test_a_recalled_short_is_refused_instead_of_opening_a_new_long() -> None:
    """THE failure this guard exists for.

    IBKR reports flat because the short was bought in. The plan's BUY would
    open a fresh long on top of the CCF long. Refuse.
    """
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader({BrokerName.FUBON_CCF: 3.0, BrokerName.IBKR_UMC: 0.0}),
    )

    assert not result.passed
    failure = result.failures()[0]
    assert failure.broker is BrokerName.IBKR_UMC
    assert "would OPEN a position" in failure.detail


def test_a_partially_recalled_short_is_refused_rather_than_resized() -> None:
    """Half the short was bought in. Sending the full size would overshoot into
    a long; silently resizing would trade on a model we have just disproved."""
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader({BrokerName.FUBON_CCF: 3.0, BrokerName.IBKR_UMC: -600.0}),
    )

    assert not result.passed
    assert "broker holds -600" in result.failures()[0].detail


def test_a_position_on_the_wrong_side_is_refused() -> None:
    """Long where a short should be: the BUY would double the long."""
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader({BrokerName.FUBON_CCF: 3.0, BrokerName.IBKR_UMC: 1200.0}),
    )

    assert not result.passed


def test_an_unreadable_position_is_refused_not_treated_as_flat() -> None:
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader({BrokerName.FUBON_CCF: 3.0, BrokerName.IBKR_UMC: None}),
    )

    assert not result.passed
    assert "did not report a position" in result.failures()[0].detail


def test_a_raising_position_query_is_refused_not_swallowed() -> None:
    def read(broker, _symbol):
        if broker is BrokerName.IBKR_UMC:
            raise ConnectionError("gateway down")
        return 3.0

    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(), read_position=read
    )

    assert not result.passed
    assert "ConnectionError" in result.failures()[0].detail


def test_entry_requires_both_brokers_to_be_flat() -> None:
    entry = plan(
        ExecutionPlanType.ENTRY,
        leg(BrokerName.FUBON_CCF, "CCFG6", OrderSide.BUY, 3),
        leg(BrokerName.IBKR_UMC, "UMC", OrderSide.SELL, 1200),
    )

    ok = verify_plan_against_broker(
        entry,
        read_position=reader({BrokerName.FUBON_CCF: 0.0, BrokerName.IBKR_UMC: 0.0}),
    )
    assert ok.passed

    # An unexpected holding means entering would stack on top of a position the
    # system does not know it has.
    stacked = verify_plan_against_broker(
        entry,
        read_position=reader({BrokerName.FUBON_CCF: 2.0, BrokerName.IBKR_UMC: 0.0}),
    )
    assert not stacked.passed
    assert "already holds +2" in stacked.failures()[0].detail


def test_tolerance_admits_float_dust_on_the_share_leg() -> None:
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader(
            {BrokerName.FUBON_CCF: 3.0, BrokerName.IBKR_UMC: -1200.0000001}
        ),
        tolerances={BrokerName.IBKR_UMC: 1e-6},
    )

    assert result.passed


def test_contract_leg_gets_no_tolerance_by_default() -> None:
    """Futures contracts are integers; being one out is a real discrepancy."""
    result = verify_plan_against_broker(
        exit_plan_closing_a_short_umc(),
        read_position=reader({BrokerName.FUBON_CCF: 2.0, BrokerName.IBKR_UMC: -1200.0}),
    )

    assert not result.passed


def test_signed_close_quantity_expects_the_opposite_side() -> None:
    assert signed_close_quantity(OrderSide.BUY, 1200) == -1200
    assert signed_close_quantity(OrderSide.SELL, 3) == 3


def test_adapter_reader_reports_none_when_the_adapter_cannot_be_asked() -> None:
    class Silent:
        pass

    class Talkative:
        def fetch_position_quantity(self) -> float:
            return -5.0

    read = adapter_position_reader(
        {BrokerName.FUBON_CCF: Talkative(), BrokerName.IBKR_UMC: Silent()}
    )

    assert read(BrokerName.FUBON_CCF, "CCFG6") == -5.0
    # None, not 0.0: an adapter that cannot say what it holds is not flat.
    assert read(BrokerName.IBKR_UMC, "UMC") is None
