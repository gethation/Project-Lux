from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.execution_intent import (
    ExecutionLeg,
    ExecutionPlanStatus,
    ExecutionPlanType,
    PairExecutionPlan,
    expected_leg_sides,
    pair_execution_plan_from_order_requests,
    validate_pair_execution_plan,
)
from lux_trader.models import BrokerName, Direction, OrderRequest, OrderSide


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


def leg(
    broker: BrokerName,
    symbol: str,
    side: OrderSide,
    quantity: float,
    price: float,
) -> ExecutionLeg:
    return ExecutionLeg(
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=ts(),
        row_index=88,
        qff_symbol="QFFG6",
        qff_expiry="2026-02-18",
        contract_policy_state="active",
    )


def short_entry_plan(*, legs: tuple[ExecutionLeg, ...] | None = None) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id="EXEC-TEST",
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_TSM_LONG_QFF,
        timestamp=ts(),
        row_index=88,
        legs=legs
        or (
            leg(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", OrderSide.SELL, 125.5, 720.0),
            leg(BrokerName.FUBON_QFF, "QFFG6", OrderSide.BUY, 3, 1180.0),
        ),
        reason="entry_zscore_crossed",
        decision_zscore=2.14,
        decision_spread_type="shortSpread",
        qff_symbol="QFFG6",
        qff_expiry="2026-02-18",
        contract_policy_state="active",
    )


def failed_check_types(plan: PairExecutionPlan) -> set[str]:
    return {check.check_type for check in plan.checks if not check.passed}


def test_valid_short_entry_plan_passes_validation() -> None:
    validated = validate_pair_execution_plan(short_entry_plan())

    assert validated.status == ExecutionPlanStatus.VALIDATED
    assert validated.accepted
    assert failed_check_types(validated) == set()


@pytest.mark.parametrize(
    ("plan_type", "direction", "expected"),
    [
        (
            ExecutionPlanType.ENTRY,
            Direction.SHORT_TSM_LONG_QFF,
            {
                BrokerName.BINANCE_TSM: OrderSide.SELL,
                BrokerName.FUBON_QFF: OrderSide.BUY,
            },
        ),
        (
            ExecutionPlanType.ENTRY,
            Direction.LONG_TSM_SHORT_QFF,
            {
                BrokerName.BINANCE_TSM: OrderSide.BUY,
                BrokerName.FUBON_QFF: OrderSide.SELL,
            },
        ),
        (
            ExecutionPlanType.EXIT,
            Direction.SHORT_TSM_LONG_QFF,
            {
                BrokerName.BINANCE_TSM: OrderSide.BUY,
                BrokerName.FUBON_QFF: OrderSide.SELL,
            },
        ),
        (
            ExecutionPlanType.EXIT,
            Direction.LONG_TSM_SHORT_QFF,
            {
                BrokerName.BINANCE_TSM: OrderSide.SELL,
                BrokerName.FUBON_QFF: OrderSide.BUY,
            },
        ),
    ],
)
def test_expected_sides_cover_entry_and_exit(
    plan_type: ExecutionPlanType,
    direction: Direction,
    expected: dict[BrokerName, OrderSide],
) -> None:
    assert expected_leg_sides(plan_type, direction) == expected


def test_missing_leg_is_rejected() -> None:
    validated = validate_pair_execution_plan(
        short_entry_plan(
            legs=(
                leg(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", OrderSide.SELL, 125.5, 720.0),
            )
        )
    )

    assert validated.status == ExecutionPlanStatus.REJECTED
    assert {"leg_count", "required_brokers"} <= failed_check_types(validated)


def test_wrong_side_is_rejected() -> None:
    validated = validate_pair_execution_plan(
        short_entry_plan(
            legs=(
                leg(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", OrderSide.BUY, 125.5, 720.0),
                leg(BrokerName.FUBON_QFF, "QFFG6", OrderSide.BUY, 3, 1180.0),
            )
        )
    )

    assert validated.status == ExecutionPlanStatus.REJECTED
    assert "side_matches_direction" in failed_check_types(validated)


def test_bad_quantity_price_and_non_integer_qff_contracts_are_rejected() -> None:
    validated = validate_pair_execution_plan(
        short_entry_plan(
            legs=(
                leg(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", OrderSide.SELL, 0, 720.0),
                leg(BrokerName.FUBON_QFF, "QFFG6", OrderSide.BUY, 1.5, -1180.0),
            )
        )
    )

    failed = failed_check_types(validated)
    assert validated.status == ExecutionPlanStatus.REJECTED
    assert {"quantity_positive", "price_positive", "qff_quantity_integer"} <= failed


def test_qff_symbol_mismatch_is_rejected() -> None:
    validated = validate_pair_execution_plan(
        short_entry_plan(
            legs=(
                leg(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", OrderSide.SELL, 125.5, 720.0),
                leg(BrokerName.FUBON_QFF, "QFFH6", OrderSide.BUY, 3, 1180.0),
            )
        )
    )

    assert validated.status == ExecutionPlanStatus.REJECTED
    assert "qff_symbol_matches" in failed_check_types(validated)


def test_allow_live_order_rejects_dry_run_intent() -> None:
    validated = validate_pair_execution_plan(short_entry_plan(), allow_live_order=True)

    assert validated.status == ExecutionPlanStatus.REJECTED
    assert "live_order_disabled" in failed_check_types(validated)


def test_build_plan_from_order_requests_preserves_metadata() -> None:
    requests = (
        OrderRequest(
            broker=BrokerName.BINANCE_TSM,
            symbol="TSM/USDT:USDT",
            side=OrderSide.SELL,
            quantity=125.5,
            price=720.0,
            timestamp=ts(),
            row_index=88,
            fee_twd=12.3,
            qff_symbol="QFFG6",
            qff_expiry="2026-02-18",
            contract_policy_state="active",
        ),
        OrderRequest(
            broker=BrokerName.FUBON_QFF,
            symbol="QFFG6",
            side=OrderSide.BUY,
            quantity=3,
            price=1180.0,
            timestamp=ts(),
            row_index=88,
            fee_twd=45.6,
            qff_symbol="QFFG6",
            qff_expiry="2026-02-18",
            contract_policy_state="active",
        ),
    )

    plan = pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_TSM_LONG_QFF,
        requests=requests,
        reason="entry_zscore_crossed",
        decision_zscore=2.14,
        decision_spread_type="shortSpread",
    )
    validated = validate_pair_execution_plan(plan)

    assert plan.row_index == 88
    assert plan.qff_symbol == "QFFG6"
    assert plan.qff_expiry == "2026-02-18"
    assert plan.contract_policy_state == "active"
    assert plan.to_jsonable()["decision_spread_type"] == "shortSpread"
    assert validated.status == ExecutionPlanStatus.VALIDATED
