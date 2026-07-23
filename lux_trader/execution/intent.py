from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Iterable

from ..core.models import (
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


class ExecutionOrderType(StrEnum):
    MARKET = "market"


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
    tw_leg_symbol: str | None = None
    tw_leg_expiry: str | None = None
    contract_policy_state: str | None = None
    order_type: str = ExecutionOrderType.MARKET.value
    expected_price: float | None = None
    trigger_bid: float | None = None
    trigger_ask: float | None = None
    trigger_mid: float | None = None
    price_source: str | None = None
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
    tw_leg_symbol: str | None = None
    tw_leg_expiry: str | None = None
    contract_policy_state: str | None = None
    order_type: str = ExecutionOrderType.MARKET.value
    price_policy: str | None = None
    plan_age_seconds: float | None = None
    max_plan_age_seconds: int | None = None
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

        add(
            "order_type_supported",
            plan.order_type == ExecutionOrderType.MARKET.value,
            "first live execution policy only supports market orders",
            payload={"order_type": plan.order_type},
        )

        broker_counts: dict[BrokerName, int] = {}
        for leg in plan.legs:
            broker_counts[leg.broker] = broker_counts.get(leg.broker, 0) + 1
        add(
            "required_brokers",
            broker_counts == {BrokerName.BINANCE: 1, BrokerName.FUBON: 1},
            "pair execution plan must contain one Binance leg and one Fubon leg",
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
            add(
                "leg_order_type_matches_plan",
                leg.order_type == plan.order_type,
                "execution leg order type must match the parent plan",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={
                    "plan_order_type": plan.order_type,
                    "leg_order_type": leg.order_type,
                },
            )
            if plan.price_policy is not None:
                add(
                    "expected_price_positive",
                    _is_positive_number(leg.expected_price),
                    "execution leg expected price must be positive for live price policy",
                    broker=leg.broker,
                    symbol=leg.symbol,
                    payload={"expected_price": leg.expected_price},
                )
                add(
                    "trigger_book_present",
                    _is_positive_number(leg.trigger_bid)
                    and _is_positive_number(leg.trigger_ask),
                    "execution leg must record trigger bid and ask",
                    broker=leg.broker,
                    symbol=leg.symbol,
                    payload={
                        "trigger_bid": leg.trigger_bid,
                        "trigger_ask": leg.trigger_ask,
                    },
                )

        us_leg_leg = _single_leg(plan.legs, BrokerName.BINANCE)
        tw_leg_leg = _single_leg(plan.legs, BrokerName.FUBON)

        if tw_leg_leg is not None:
            add(
                "tw_leg_quantity_integer",
                _is_integer_quantity(tw_leg_leg.quantity),
                f"Fubon {tw_leg_leg.symbol} quantity must be an integer number of contracts",
                broker=tw_leg_leg.broker,
                symbol=tw_leg_leg.symbol,
                payload={"quantity": tw_leg_leg.quantity},
            )
            add(
                "tw_leg_symbol_present",
                bool(plan.tw_leg_symbol),
                f"pair execution plan must include active symbol {tw_leg_leg.symbol}",
                broker=tw_leg_leg.broker,
                symbol=tw_leg_leg.symbol,
            )
            if plan.tw_leg_symbol:
                add(
                    "tw_leg_symbol_matches",
                    tw_leg_leg.symbol == plan.tw_leg_symbol,
                    f"Fubon symbol must match active symbol {plan.tw_leg_symbol}",
                    broker=tw_leg_leg.broker,
                    symbol=tw_leg_leg.symbol,
                    payload={
                        "expected_tw_leg_symbol": plan.tw_leg_symbol,
                        "actual_symbol": tw_leg_leg.symbol,
                    },
                )

        expected_sides = expected_leg_sides(plan.plan_type, plan.direction)
        for broker, expected_side in expected_sides.items():
            leg = us_leg_leg if broker == BrokerName.BINANCE else tw_leg_leg
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
        Direction.SHORT_US_LONG_TW: {
            BrokerName.BINANCE: OrderSide.SELL,
            BrokerName.FUBON: OrderSide.BUY,
        },
        Direction.LONG_US_SHORT_TW: {
            BrokerName.BINANCE: OrderSide.BUY,
            BrokerName.FUBON: OrderSide.SELL,
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
        tw_leg_symbol=request.tw_leg_symbol,
        tw_leg_expiry=request.tw_leg_expiry,
        contract_policy_state=request.contract_policy_state,
        order_type=request.order_type,
        expected_price=request.expected_price,
        trigger_bid=request.trigger_bid,
        trigger_ask=request.trigger_ask,
        trigger_mid=request.trigger_mid,
        price_source=request.price_source,
        raw={"source": "order_request"},
    )


def execution_leg_from_jsonable(payload: dict[str, Any]) -> ExecutionLeg:
    return ExecutionLeg(
        broker=BrokerName(payload["broker"]),
        symbol=str(payload["symbol"]),
        side=OrderSide(payload["side"]),
        quantity=float(payload["quantity"]),
        price=float(payload["price"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        row_index=int(payload["row_index"]),
        fee_twd=float(payload.get("fee_twd", 0.0)),
        tw_leg_symbol=payload.get("tw_leg_symbol"),
        tw_leg_expiry=payload.get("tw_leg_expiry"),
        contract_policy_state=payload.get("contract_policy_state"),
        order_type=str(payload.get("order_type", ExecutionOrderType.MARKET.value)),
        expected_price=payload.get("expected_price"),
        trigger_bid=payload.get("trigger_bid"),
        trigger_ask=payload.get("trigger_ask"),
        trigger_mid=payload.get("trigger_mid"),
        price_source=payload.get("price_source"),
        raw=payload.get("raw"),
    )


def execution_check_from_jsonable(payload: dict[str, Any]) -> ExecutionCheck:
    broker = payload.get("broker")
    return ExecutionCheck(
        check_type=str(payload["check_type"]),
        passed=bool(payload["passed"]),
        message=str(payload["message"]),
        broker=BrokerName(broker) if broker else None,
        symbol=payload.get("symbol"),
        payload=payload.get("payload"),
    )


def pair_execution_plan_from_jsonable(payload: dict[str, Any]) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id=str(payload["plan_id"]),
        plan_type=ExecutionPlanType(payload["plan_type"]),
        direction=Direction(payload["direction"]),
        timestamp=datetime.fromisoformat(str(payload["timestamp"])),
        row_index=int(payload["row_index"]),
        legs=tuple(execution_leg_from_jsonable(leg) for leg in payload.get("legs", [])),
        status=ExecutionPlanStatus(payload.get("status", ExecutionPlanStatus.CREATED.value)),
        reason=str(payload.get("reason", "")),
        decision_zscore=payload.get("decision_zscore"),
        decision_spread_type=payload.get("decision_spread_type"),
        tw_leg_symbol=payload.get("tw_leg_symbol"),
        tw_leg_expiry=payload.get("tw_leg_expiry"),
        contract_policy_state=payload.get("contract_policy_state"),
        order_type=str(payload.get("order_type", ExecutionOrderType.MARKET.value)),
        price_policy=payload.get("price_policy"),
        plan_age_seconds=payload.get("plan_age_seconds"),
        max_plan_age_seconds=payload.get("max_plan_age_seconds"),
        checks=tuple(
            execution_check_from_jsonable(check)
            for check in payload.get("checks", [])
        ),
    )


def pair_execution_plan_from_order_requests(
    *,
    plan_type: ExecutionPlanType,
    direction: Direction,
    requests: Iterable[OrderRequest],
    reason: str = "",
    decision_zscore: float | None = None,
    decision_spread_type: str | None = None,
    order_type: str = ExecutionOrderType.MARKET.value,
    price_policy: str | None = None,
    plan_age_seconds: float | None = None,
    max_plan_age_seconds: int | None = None,
    plan_id: str | None = None,
) -> PairExecutionPlan:
    legs = tuple(execution_leg_from_order_request(request) for request in requests)
    if not legs:
        raise ValueError("requests must contain at least one order request")
    timestamp = legs[0].timestamp
    row_index = legs[0].row_index
    tw_leg_leg = _single_leg(legs, BrokerName.FUBON)
    tw_leg_symbol = None
    tw_leg_expiry = None
    contract_policy_state = None
    if tw_leg_leg is not None:
        tw_leg_symbol = tw_leg_leg.tw_leg_symbol or tw_leg_leg.symbol
        tw_leg_expiry = tw_leg_leg.tw_leg_expiry
        contract_policy_state = tw_leg_leg.contract_policy_state
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
        tw_leg_symbol=tw_leg_symbol,
        tw_leg_expiry=tw_leg_expiry,
        contract_policy_state=contract_policy_state,
        order_type=order_type,
        price_policy=price_policy,
        plan_age_seconds=plan_age_seconds,
        max_plan_age_seconds=max_plan_age_seconds,
    )


def _single_leg(
    legs: Iterable[ExecutionLeg],
    broker: BrokerName,
) -> ExecutionLeg | None:
    matches = [leg for leg in legs if leg.broker == broker]
    return matches[0] if len(matches) == 1 else None


def _is_positive_number(value: float | None) -> bool:
    if value is None:
        return False
    return isfinite(float(value)) and float(value) > 0.0


def _is_integer_quantity(value: float) -> bool:
    numeric = float(value)
    return isfinite(numeric) and numeric > 0.0 and numeric.is_integer()
