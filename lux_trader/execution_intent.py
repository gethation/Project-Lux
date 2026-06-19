from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Iterable

from .models import (
    BrokerName,
    Direction,
    OrderRequest,
    OrderSide,
    dataclass_to_jsonable,
)


class ExecutionPlanType(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


class ExecutionPlanStatus(StrEnum):
    CREATED = "created"
    VALIDATED = "validated"
    REJECTED = "rejected"
    RECORDED = "recorded"


@dataclass(frozen=True)
class ExecutionLeg:
    broker: BrokerName
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    timestamp: datetime
    row_index: int
    fee_twd: float = 0.0
    qff_symbol: str | None = None
    qff_expiry: str | None = None
    contract_policy_state: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionCheck:
    check_type: str
    passed: bool
    message: str
    broker: BrokerName | None = None
    symbol: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class PairExecutionPlan:
    plan_id: str
    plan_type: ExecutionPlanType
    direction: Direction
    timestamp: datetime
    row_index: int
    legs: tuple[ExecutionLeg, ...]
    status: ExecutionPlanStatus = ExecutionPlanStatus.CREATED
    reason: str = ""
    decision_zscore: float | None = None
    decision_spread_type: str | None = None
    qff_symbol: str | None = None
    qff_expiry: str | None = None
    contract_policy_state: str | None = None
    checks: tuple[ExecutionCheck, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status in {
            ExecutionPlanStatus.VALIDATED,
            ExecutionPlanStatus.RECORDED,
        }

    def to_jsonable(self) -> dict[str, Any]:
        return dataclass_to_jsonable(self)


class PairExecutionPlanValidator:
    def __init__(self, *, allow_live_order: bool = False) -> None:
        self.allow_live_order = bool(allow_live_order)

    def validate(self, plan: PairExecutionPlan) -> PairExecutionPlan:
        checks: list[ExecutionCheck] = []

        def add(
            check_type: str,
            passed: bool,
            message: str,
            *,
            broker: BrokerName | None = None,
            symbol: str | None = None,
            payload: dict[str, Any] | None = None,
        ) -> None:
            checks.append(
                ExecutionCheck(
                    check_type=check_type,
                    passed=passed,
                    message=message,
                    broker=broker,
                    symbol=symbol,
                    payload=payload,
                )
            )

        add(
            "live_order_disabled",
            not self.allow_live_order,
            "allow_live_order must remain false for dry-run execution intent",
        )

        add(
            "leg_count",
            len(plan.legs) == 2,
            "pair execution plan must contain exactly two legs",
            payload={"actual_leg_count": len(plan.legs)},
        )

        broker_counts: dict[BrokerName, int] = {}
        for leg in plan.legs:
            broker_counts[leg.broker] = broker_counts.get(leg.broker, 0) + 1
        add(
            "required_brokers",
            broker_counts == {BrokerName.BINANCE_TSM: 1, BrokerName.FUBON_QFF: 1},
            "pair execution plan must contain one Binance TSM leg and one Fubon QFF leg",
            payload={broker.value: count for broker, count in broker_counts.items()},
        )

        for leg in plan.legs:
            add(
                "quantity_positive",
                _is_positive_number(leg.quantity),
                "execution leg quantity must be positive and finite",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={"quantity": leg.quantity},
            )
            add(
                "price_positive",
                _is_positive_number(leg.price),
                "execution leg price must be positive and finite",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={"price": leg.price},
            )
            add(
                "same_timestamp",
                leg.timestamp == plan.timestamp and leg.row_index == plan.row_index,
                "execution leg timestamp and row_index must match the parent plan",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={
                    "plan_timestamp": plan.timestamp.isoformat(),
                    "leg_timestamp": leg.timestamp.isoformat(),
                    "plan_row_index": plan.row_index,
                    "leg_row_index": leg.row_index,
                },
            )

        tsm_leg = _single_leg(plan.legs, BrokerName.BINANCE_TSM)
        qff_leg = _single_leg(plan.legs, BrokerName.FUBON_QFF)

        if qff_leg is not None:
            add(
                "qff_quantity_integer",
                _is_integer_quantity(qff_leg.quantity),
                "Fubon QFF quantity must be an integer number of contracts",
                broker=qff_leg.broker,
                symbol=qff_leg.symbol,
                payload={"quantity": qff_leg.quantity},
            )
            add(
                "qff_symbol_present",
                bool(plan.qff_symbol),
                "pair execution plan must include the active QFF symbol",
                broker=qff_leg.broker,
                symbol=qff_leg.symbol,
            )
            if plan.qff_symbol:
                add(
                    "qff_symbol_matches",
                    qff_leg.symbol == plan.qff_symbol,
                    "Fubon QFF leg symbol must match the active QFF symbol",
                    broker=qff_leg.broker,
                    symbol=qff_leg.symbol,
                    payload={
                        "expected_qff_symbol": plan.qff_symbol,
                        "actual_symbol": qff_leg.symbol,
                    },
                )

        expected_sides = expected_leg_sides(plan.plan_type, plan.direction)
        for broker, expected_side in expected_sides.items():
            leg = tsm_leg if broker == BrokerName.BINANCE_TSM else qff_leg
            if leg is None:
                continue
            add(
                "side_matches_direction",
                leg.side == expected_side,
                "execution leg side must match plan type and strategy direction",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={
                    "plan_type": plan.plan_type.value,
                    "direction": plan.direction.value,
                    "expected_side": expected_side.value,
                    "actual_side": leg.side.value,
                },
            )

        status = (
            ExecutionPlanStatus.VALIDATED
            if all(check.passed for check in checks)
            else ExecutionPlanStatus.REJECTED
        )
        return replace(plan, status=status, checks=tuple(checks))


def expected_leg_sides(
    plan_type: ExecutionPlanType,
    direction: Direction,
) -> dict[BrokerName, OrderSide]:
    entry_sides = {
        Direction.SHORT_TSM_LONG_QFF: {
            BrokerName.BINANCE_TSM: OrderSide.SELL,
            BrokerName.FUBON_QFF: OrderSide.BUY,
        },
        Direction.LONG_TSM_SHORT_QFF: {
            BrokerName.BINANCE_TSM: OrderSide.BUY,
            BrokerName.FUBON_QFF: OrderSide.SELL,
        },
    }[direction]
    if plan_type == ExecutionPlanType.ENTRY:
        return entry_sides
    return {
        broker: OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        for broker, side in entry_sides.items()
    }


def validate_pair_execution_plan(
    plan: PairExecutionPlan,
    *,
    allow_live_order: bool = False,
) -> PairExecutionPlan:
    return PairExecutionPlanValidator(allow_live_order=allow_live_order).validate(plan)


def make_execution_plan_id(
    *,
    plan_type: ExecutionPlanType,
    direction: Direction,
    timestamp: datetime,
    row_index: int,
) -> str:
    compact_time = timestamp.strftime("%Y%m%d%H%M%S")
    return f"EXEC-{row_index:08d}-{compact_time}-{plan_type.value}-{direction.value}"


def execution_leg_from_order_request(request: OrderRequest) -> ExecutionLeg:
    return ExecutionLeg(
        broker=request.broker,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        price=request.price,
        timestamp=request.timestamp,
        row_index=request.row_index,
        fee_twd=request.fee_twd,
        qff_symbol=request.qff_symbol,
        qff_expiry=request.qff_expiry,
        contract_policy_state=request.contract_policy_state,
        raw={"source": "order_request"},
    )


def pair_execution_plan_from_order_requests(
    *,
    plan_type: ExecutionPlanType,
    direction: Direction,
    requests: Iterable[OrderRequest],
    reason: str = "",
    decision_zscore: float | None = None,
    decision_spread_type: str | None = None,
    plan_id: str | None = None,
) -> PairExecutionPlan:
    legs = tuple(execution_leg_from_order_request(request) for request in requests)
    if not legs:
        raise ValueError("requests must contain at least one order request")
    timestamp = legs[0].timestamp
    row_index = legs[0].row_index
    qff_leg = _single_leg(legs, BrokerName.FUBON_QFF)
    qff_symbol = None
    qff_expiry = None
    contract_policy_state = None
    if qff_leg is not None:
        qff_symbol = qff_leg.qff_symbol or qff_leg.symbol
        qff_expiry = qff_leg.qff_expiry
        contract_policy_state = qff_leg.contract_policy_state
    return PairExecutionPlan(
        plan_id=plan_id
        or make_execution_plan_id(
            plan_type=plan_type,
            direction=direction,
            timestamp=timestamp,
            row_index=row_index,
        ),
        plan_type=plan_type,
        direction=direction,
        timestamp=timestamp,
        row_index=row_index,
        legs=legs,
        reason=reason,
        decision_zscore=decision_zscore,
        decision_spread_type=decision_spread_type,
        qff_symbol=qff_symbol,
        qff_expiry=qff_expiry,
        contract_policy_state=contract_policy_state,
    )


def _single_leg(
    legs: Iterable[ExecutionLeg],
    broker: BrokerName,
) -> ExecutionLeg | None:
    matches = [leg for leg in legs if leg.broker == broker]
    return matches[0] if len(matches) == 1 else None


def _is_positive_number(value: float) -> bool:
    return isfinite(float(value)) and float(value) > 0.0


def _is_integer_quantity(value: float) -> bool:
    numeric = float(value)
    return isfinite(numeric) and numeric > 0.0 and numeric.is_integer()
