"""The recall unwind policy.

The system's only path that places an order in an abnormal state, so most of
these tests are about what it REFUSES to do.
"""

from __future__ import annotations

import pytest

from lux_trader.core.models import OrderSide
from lux_trader.execution.recall_response import (
    ACTION_CLOSE_ALL,
    ACTION_NOTHING,
    ACTION_REDUCE,
    ACTION_REFUSE,
    hedge_target_contracts,
    plan_recall_response,
    recall_budget_exhausted,
)


# The entry: long 3 CCF against short 1,200 UMC shares.
ENTRY = {"entry_ccf_contracts": 3, "entry_umc_units": -1200.0}


def response(observed_ccf: float, observed_umc: float):
    return plan_recall_response(
        **ENTRY,
        observed_ccf_contracts=observed_ccf,
        observed_umc_units=observed_umc,
    )


def test_a_full_recall_closes_the_whole_ccf_leg() -> None:
    """UMC is gone, so the CCF long is outright directional. Close all of it."""
    result = response(3, 0.0)

    assert result.action == ACTION_CLOSE_ALL
    assert result.target_ccf_contracts == 0
    assert result.close_contracts == 3
    assert result.close_side == OrderSide.SELL  # closing a long sells


def test_a_partial_recall_reduces_proportionally() -> None:
    """Two thirds of the short bought in: 3 contracts -> 1."""
    result = response(3, -400.0)

    assert result.action == ACTION_REDUCE
    assert result.target_ccf_contracts == 1
    assert result.close_contracts == 2


def test_an_intact_position_needs_no_order() -> None:
    result = response(3, -1200.0)

    assert result.action == ACTION_NOTHING
    assert result.close_contracts == 0
    assert not result.requires_order


def test_a_short_ccf_leg_is_closed_by_buying() -> None:
    result = plan_recall_response(
        entry_ccf_contracts=-3,
        entry_umc_units=1200.0,
        observed_ccf_contracts=-3,
        observed_umc_units=0.0,
    )

    assert result.action == ACTION_CLOSE_ALL
    assert result.close_side == OrderSide.BUY


# --- assertion 1: close only -------------------------------------------------


def test_it_refuses_to_increase_a_position() -> None:
    """If UMC somehow GREW, the arithmetic wants more CCF. An emergency unwind
    that can open a position is not an unwind."""
    result = response(1, -1200.0)  # holds 1, ratio implies 3

    assert result.action == ACTION_REFUSE
    assert "must never increase" in result.reason
    assert not result.requires_order


def test_it_refuses_to_flip_a_position() -> None:
    result = plan_recall_response(
        entry_ccf_contracts=3,
        entry_umc_units=-1200.0,
        observed_ccf_contracts=3,
        observed_umc_units=1200.0,  # sign flipped
    )

    assert result.action == ACTION_REFUSE
    assert "must never reverse" in result.reason


def test_it_refuses_a_fractional_contract_report() -> None:
    """CCF trades in whole contracts; a fraction means the report is wrong, and
    sizing an emergency order off a wrong report is how one bug becomes two."""
    result = response(2.5, 0.0)

    assert result.action == ACTION_REFUSE
    assert "not whole" in result.reason


def test_it_refuses_without_a_hedge_ratio() -> None:
    result = plan_recall_response(
        entry_ccf_contracts=3,
        entry_umc_units=0.0,
        observed_ccf_contracts=3,
        observed_umc_units=0.0,
    )

    assert result.action == ACTION_REFUSE


def test_nothing_to_unwind_is_not_a_refusal() -> None:
    """Both legs already gone is a clean state, not an error."""
    result = response(0, 0.0)

    assert result.action == ACTION_NOTHING
    assert not result.refused


# --- assertion 2: sized from the broker --------------------------------------


def test_the_close_is_sized_from_the_broker_not_the_entry() -> None:
    """Internal state says 3 contracts; Fubon says 2, because something else
    already moved. The order must close 2."""
    result = response(2, 0.0)

    assert result.close_contracts == 2
    assert result.target_ccf_contracts == 0


# --- rounding ----------------------------------------------------------------


def test_the_hedge_target_rounds_to_nearest_contract() -> None:
    # Rounding down leaves the pair net short the US leg, rounding up net long;
    # nearest minimises the residual either way.
    assert hedge_target_contracts(
        entry_ccf_contracts=3, entry_umc_units=-1200.0, remaining_umc_units=-600.0
    ) == 2  # exact 1.5 -> away from zero
    assert hedge_target_contracts(
        entry_ccf_contracts=3, entry_umc_units=-1200.0, remaining_umc_units=-500.0
    ) == 1  # exact 1.25
    assert hedge_target_contracts(
        entry_ccf_contracts=-4, entry_umc_units=1600.0, remaining_umc_units=800.0
    ) == -2


# --- the recall loop ---------------------------------------------------------


def test_the_daily_recall_budget_stops_a_loop() -> None:
    """Returning to FLAT means the strategy may re-enter the same unborrowable
    short and be recalled again, paying fees each round."""
    assert not recall_budget_exhausted(0, 2)
    assert not recall_budget_exhausted(1, 2)
    assert recall_budget_exhausted(2, 2)
    assert recall_budget_exhausted(5, 2)


def test_a_zero_limit_still_allows_the_first_unwind() -> None:
    """The floor of 1 is load-bearing. A limit of 0 taken literally would refuse
    to unwind the very first recall, leaving the naked leg the whole mechanism
    exists to remove -- a misconfiguration that makes things worse than having
    no recall handling at all."""
    assert not recall_budget_exhausted(0, 0)
    assert recall_budget_exhausted(1, 0)
    assert not recall_budget_exhausted(0, 1)


# --- the close-only plan the unwind actually sends ---------------------------


def test_the_unwind_plan_is_a_single_close_only_ccf_leg() -> None:
    """Routed through the ordinary plan/adapter path on purpose: the recall
    unwind then gets the same recording, fill confirmation and audit trail as
    every other order. An emergency path with its own plumbing is one nobody
    has tested."""
    from datetime import datetime

    from lux_trader.core.models import BrokerName
    from lux_trader.execution.intent import ExecutionPlanType
    from lux_trader.execution.recall_response import build_recall_unwind_plan

    ts = datetime.fromisoformat("2026-07-30T00:30:00+08:00")
    plan = build_recall_unwind_plan(
        response(3, 0.0), ccf_symbol="CCFG6", timestamp=ts
    )

    assert plan.plan_type == ExecutionPlanType.EXIT
    assert len(plan.legs) == 1
    leg = plan.legs[0]
    assert leg.broker == BrokerName.FUBON_CCF
    assert leg.symbol == "CCFG6"
    assert leg.side == OrderSide.SELL
    assert leg.quantity == 3.0
    assert plan.reason == "umc_recall_unwind"


def test_building_a_plan_from_a_no_order_response_is_an_error() -> None:
    """A refusal or a no-op must never silently become an order."""
    from datetime import datetime

    from lux_trader.execution.recall_response import build_recall_unwind_plan

    ts = datetime.fromisoformat("2026-07-30T00:30:00+08:00")
    for no_order in (response(3, -1200.0), response(1, -1200.0)):
        with pytest.raises(ValueError, match="no order to build"):
            build_recall_unwind_plan(no_order, ccf_symbol="CCFG6", timestamp=ts)


def test_the_store_counts_recalls_per_day(tmp_path) -> None:
    """The daily budget reads this; a wrong count either loops or stops early."""
    from datetime import datetime

    from lux_trader.store import SQLiteStore

    store = SQLiteStore(tmp_path / "recalls.sqlite3")
    try:
        store.initialize()
        day = datetime.fromisoformat("2026-07-30T01:00:00+08:00")
        later_same_day = datetime.fromisoformat("2026-07-30T23:00:00+08:00")
        next_day = datetime.fromisoformat("2026-07-31T01:00:00+08:00")

        assert store.count_events_on(day, "umc_recall_unwound") == 0
        store.record_event(-1, day, "umc_recall_unwound", "one", {})
        store.record_event(-1, later_same_day, "umc_recall_unwound", "two", {})
        store.record_event(-1, next_day, "umc_recall_unwound", "next", {})
        # A different event type on the same day must not count.
        store.record_event(-1, day, "margin_check", "unrelated", {})
        store.commit()

        assert store.count_events_on(day, "umc_recall_unwound") == 2
        assert store.count_events_on(next_day, "umc_recall_unwound") == 1
    finally:
        store.close()
