from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .execution import ExecutionAdapter, ExecutionOutcome, ExecutionOutcomeStatus
from .execution_intent import (
    ExecutionCheck,
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanStatus,
    ExecutionPlanType,
    PairExecutionPlan,
    expected_leg_sides,
)
from .models import BrokerName, Fill, OrderSide, StrategyState


@dataclass(frozen=True)
class RecordedExecutionEvent:
    event_type: str
    message: str
    payload: dict[str, Any]


class RealExecutionCoordinator:
    def __init__(
        self,
        *,
        store: Any,
        binance_adapter: ExecutionAdapter,
        fubon_adapter: ExecutionAdapter,
        qff_first: bool = True,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.adapters = {
            BrokerName.BINANCE_TSM: binance_adapter,
            BrokerName.FUBON_QFF: fubon_adapter,
        }
        self.qff_first = bool(qff_first)
        self.clock = clock or (lambda: datetime.now().astimezone())

    def execute(
        self,
        plan: PairExecutionPlan,
    ) -> tuple[PairExecutionPlan, ExecutionOutcome]:
        recorded = record_live_execution_plan(self.store, plan, qff_first=self.qff_first)
        if recorded.status != ExecutionPlanStatus.RECORDED:
            outcome = ExecutionOutcome(
                plan_id=recorded.plan_id,
                timestamp=self.clock(),
                status=ExecutionOutcomeStatus.REJECTED,
                message="live execution plan rejected",
                recommended_state=StrategyState.PAUSED,
                payload={
                    "adapter": "real_execution_coordinator",
                    "failed_checks": [
                        check.to_jsonable() if hasattr(check, "to_jsonable") else {
                            "check_type": check.check_type,
                            "message": check.message,
                        }
                        for check in recorded.checks
                        if not check.passed
                    ],
                },
            )
            self.store.record_execution_outcome(outcome)
            return recorded, outcome

        qff_leg = single_leg(recorded, BrokerName.FUBON_QFF)
        binance_leg = single_leg(recorded, BrokerName.BINANCE_TSM)
        if qff_leg is None or binance_leg is None:
            outcome = ExecutionOutcome(
                plan_id=recorded.plan_id,
                timestamp=self.clock(),
                status=ExecutionOutcomeStatus.REJECTED,
                message="live execution plan missing required legs",
                recommended_state=StrategyState.PAUSED,
                payload={"adapter": "real_execution_coordinator"},
            )
            self.store.record_execution_outcome(outcome)
            return recorded, outcome

        primary_outcomes: dict[BrokerName, ExecutionOutcome] = {}
        emergency_outcomes: list[ExecutionOutcome] = []
        events: list[RecordedExecutionEvent] = []
        sequence = [qff_leg, binance_leg]

        first_leg = sequence[0]
        first_outcome = self._execute_leg(recorded, first_leg, suffix="primary")
        primary_outcomes[first_leg.broker] = first_outcome
        first_filled_quantity = filled_quantity(first_outcome, first_leg)
        if not first_outcome.filled:
            if first_filled_quantity > 0:
                emergency_outcomes.extend(
                    self._handle_exposure_breach(
                        recorded,
                        exposed_legs=[(first_leg, first_filled_quantity)],
                        failed_broker=first_leg.broker,
                        failed_status=first_outcome.status,
                        breach_type="single_leg_exposure",
                        events=events,
                    )
                )
            outcome = self._combined_outcome(
                recorded,
                status=first_outcome.status,
                message=f"live execution stopped after {first_leg.broker.value}",
                primary_outcomes=primary_outcomes,
                emergency_outcomes=emergency_outcomes,
                events=events,
            )
            self.store.record_execution_outcome(outcome)
            return recorded, outcome

        second_leg = sequence[1]
        second_outcome = self._execute_leg(recorded, second_leg, suffix="primary")
        primary_outcomes[second_leg.broker] = second_outcome
        second_filled_quantity = filled_quantity(second_outcome, second_leg)
        if second_outcome.filled:
            outcome = self._combined_outcome(
                recorded,
                status=ExecutionOutcomeStatus.FILLED,
                message="real execution pair filled",
                primary_outcomes=primary_outcomes,
                emergency_outcomes=(),
                events=events,
            )
            self.store.record_execution_outcome(outcome)
            return recorded, outcome

        exposed_legs: list[tuple[ExecutionLeg, float]] = [
            (first_leg, first_filled_quantity)
        ]
        breach_type = "single_leg_exposure"
        if second_filled_quantity > 0:
            exposed_legs.append((second_leg, second_filled_quantity))
            breach_type = "imbalanced_pair_exposure"
        emergency_outcomes.extend(
            self._handle_exposure_breach(
                recorded,
                exposed_legs=exposed_legs,
                failed_broker=second_leg.broker,
                failed_status=second_outcome.status,
                breach_type=breach_type,
                events=events,
            )
        )
        outcome = self._combined_outcome(
            recorded,
            status=second_outcome.status,
            message=f"live execution {breach_type}",
            primary_outcomes=primary_outcomes,
            emergency_outcomes=emergency_outcomes,
            events=events,
        )
        self.store.record_execution_outcome(outcome)
        return recorded, outcome

    def _execute_leg(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        *,
        suffix: str,
    ) -> ExecutionOutcome:
        leg_plan = replace(
            plan,
            plan_id=f"{plan.plan_id}-{suffix}-{leg.broker.value}",
            legs=(leg,),
            checks=(),
        )
        return self.adapters[leg.broker].execute(leg_plan)

    def _handle_exposure_breach(
        self,
        plan: PairExecutionPlan,
        *,
        exposed_legs: list[tuple[ExecutionLeg, float]],
        failed_broker: BrokerName,
        failed_status: ExecutionOutcomeStatus,
        breach_type: str,
        events: list[RecordedExecutionEvent],
    ) -> list[ExecutionOutcome]:
        exposure_payload = {
            "plan_id": plan.plan_id,
            "breach_type": breach_type,
            "failed_broker": failed_broker.value,
            "failed_status": failed_status.value,
            "exposed_legs": [
                exposure_payload_for_leg(leg, quantity)
                for leg, quantity in exposed_legs
            ],
        }
        self._record_event(
            plan,
            events,
            "exposure_breach",
            "live execution exposure breach",
            exposure_payload,
        )
        self._record_event(
            plan,
            events,
            breach_type,
            "single leg or imbalanced exposure detected",
            exposure_payload,
        )

        emergency_outcomes: list[ExecutionOutcome] = []
        for leg, quantity in exposed_legs:
            emergency_plan = emergency_close_plan(
                plan,
                leg,
                quantity=quantity,
                timestamp=self.clock(),
            )
            self._record_event(
                plan,
                events,
                "emergency_close_attempted",
                "attempting emergency close",
                {
                    "plan_id": plan.plan_id,
                    "emergency_plan_id": emergency_plan.plan_id,
                    "broker": leg.broker.value,
                    "symbol": leg.symbol,
                    "side": reverse_side(leg.side).value,
                    "quantity": quantity,
                },
            )
            outcome = self.adapters[leg.broker].execute(emergency_plan)
            emergency_outcomes.append(outcome)
            if outcome.filled:
                self._record_event(
                    plan,
                    events,
                    "emergency_close_filled",
                    "emergency close filled",
                    emergency_event_payload(emergency_plan, outcome),
                )
            else:
                payload = emergency_event_payload(emergency_plan, outcome)
                self._record_event(
                    plan,
                    events,
                    "emergency_close_failed",
                    "emergency close failed",
                    payload,
                )
                self._record_event(
                    plan,
                    events,
                    "critical_manual_intervention_required",
                    "CRITICAL manual intervention required",
                    payload,
                )
        return emergency_outcomes

    def _record_event(
        self,
        plan: PairExecutionPlan,
        events: list[RecordedExecutionEvent],
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        event = RecordedExecutionEvent(event_type, message, payload)
        events.append(event)
        self.store.record_event(
            plan.row_index,
            self.clock(),
            event_type,
            message,
            payload,
        )

    def _combined_outcome(
        self,
        plan: PairExecutionPlan,
        *,
        status: ExecutionOutcomeStatus,
        message: str,
        primary_outcomes: dict[BrokerName, ExecutionOutcome],
        emergency_outcomes: list[ExecutionOutcome] | tuple[ExecutionOutcome, ...],
        events: list[RecordedExecutionEvent],
    ) -> ExecutionOutcome:
        orders = []
        fills = []
        for outcome in list(primary_outcomes.values()) + list(emergency_outcomes):
            orders.extend(outcome.orders)
            fills.extend(outcome.fills)
        final_status = status if status != ExecutionOutcomeStatus.FILLED else status
        recommended_state = None
        if final_status != ExecutionOutcomeStatus.FILLED or emergency_outcomes:
            recommended_state = StrategyState.PAUSED
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=final_status,
            message=message,
            orders=tuple(orders),
            fills=tuple(fills),
            recommended_state=recommended_state,
            payload={
                "adapter": "real_execution_coordinator",
                "qff_first": self.qff_first,
                "primary_outcomes": {
                    broker.value: outcome.to_jsonable()
                    for broker, outcome in primary_outcomes.items()
                },
                "emergency_close_outcomes": [
                    outcome.to_jsonable() for outcome in emergency_outcomes
                ],
                "events": [
                    {
                        "event_type": event.event_type,
                        "message": event.message,
                        "payload": event.payload,
                    }
                    for event in events
                ],
                "critical": any(
                    event.event_type == "critical_manual_intervention_required"
                    for event in events
                ),
            },
        )


def record_live_execution_plan(
    store: Any,
    plan: PairExecutionPlan,
    *,
    qff_first: bool,
) -> PairExecutionPlan:
    validated = validate_live_execution_plan(plan, qff_first=qff_first)
    recorded = (
        replace(validated, status=ExecutionPlanStatus.RECORDED)
        if validated.status == ExecutionPlanStatus.VALIDATED
        else validated
    )
    store.record_execution_plan(recorded)
    return recorded


def validate_live_execution_plan(
    plan: PairExecutionPlan,
    *,
    qff_first: bool,
) -> PairExecutionPlan:
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
        "qff_first_required",
        qff_first,
        "first live execution policy requires qff_first=true",
    )
    add(
        "leg_count",
        len(plan.legs) == 2,
        "live execution plan must contain exactly two legs",
        payload={"actual_leg_count": len(plan.legs)},
    )
    add(
        "order_type_supported",
        plan.order_type == ExecutionOrderType.MARKET.value,
        "live execution only supports market orders",
        payload={"order_type": plan.order_type},
    )
    broker_counts: dict[BrokerName, int] = {}
    for leg in plan.legs:
        broker_counts[leg.broker] = broker_counts.get(leg.broker, 0) + 1
    add(
        "required_brokers",
        broker_counts == {BrokerName.BINANCE_TSM: 1, BrokerName.FUBON_QFF: 1},
        "live execution plan must contain one Binance leg and one Fubon leg",
        payload={broker.value: count for broker, count in broker_counts.items()},
    )

    expected_sides = expected_leg_sides(plan.plan_type, plan.direction)
    for leg in plan.legs:
        add(
            "quantity_positive",
            float(leg.quantity) > 0,
            "live execution leg quantity must be positive",
            broker=leg.broker,
            symbol=leg.symbol,
            payload={"quantity": leg.quantity},
        )
        add(
            "leg_order_type_matches_plan",
            leg.order_type == plan.order_type,
            "live execution leg order type must match parent plan",
            broker=leg.broker,
            symbol=leg.symbol,
            payload={"leg_order_type": leg.order_type, "plan_order_type": plan.order_type},
        )
        expected_side = expected_sides.get(leg.broker)
        if expected_side is not None:
            add(
                "side_matches_direction",
                leg.side == expected_side,
                "live execution leg side must match plan direction",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={
                    "expected_side": expected_side.value,
                    "actual_side": leg.side.value,
                },
            )
        if leg.broker == BrokerName.FUBON_QFF:
            add(
                "qff_quantity_integer",
                float(leg.quantity).is_integer(),
                "Fubon futures quantity must be an integer lot",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={"quantity": leg.quantity},
            )
            add(
                "qff_symbol_matches",
                not plan.qff_symbol or leg.symbol == plan.qff_symbol,
                "Fubon leg symbol must match plan qff_symbol",
                broker=leg.broker,
                symbol=leg.symbol,
                payload={"qff_symbol": plan.qff_symbol, "symbol": leg.symbol},
            )

    return replace(
        plan,
        status=(
            ExecutionPlanStatus.VALIDATED
            if all(check.passed for check in checks)
            else ExecutionPlanStatus.REJECTED
        ),
        checks=tuple(checks),
    )


def single_leg(plan: PairExecutionPlan, broker: BrokerName) -> ExecutionLeg | None:
    matches = [leg for leg in plan.legs if leg.broker == broker]
    return matches[0] if len(matches) == 1 else None


def filled_quantity(outcome: ExecutionOutcome, leg: ExecutionLeg) -> float:
    return sum(
        fill.quantity
        for fill in outcome.fills
        if fill.broker == leg.broker and fill.symbol == leg.symbol
    )


def emergency_close_plan(
    plan: PairExecutionPlan,
    leg: ExecutionLeg,
    *,
    quantity: float,
    timestamp: datetime,
) -> PairExecutionPlan:
    close_leg = replace(
        leg,
        side=reverse_side(leg.side),
        quantity=quantity,
        fee_twd=0.0,
        timestamp=timestamp,
        raw={
            **(leg.raw or {}),
            "source": "emergency_close",
            "original_plan_id": plan.plan_id,
        },
    )
    return PairExecutionPlan(
        plan_id=f"{plan.plan_id}-EMERGENCY-{leg.broker.value}",
        plan_type=ExecutionPlanType.EXIT,
        direction=plan.direction,
        timestamp=timestamp,
        row_index=plan.row_index,
        legs=(close_leg,),
        status=ExecutionPlanStatus.RECORDED,
        reason="emergency_close",
        decision_zscore=plan.decision_zscore,
        decision_spread_type=plan.decision_spread_type,
        qff_symbol=plan.qff_symbol,
        qff_expiry=plan.qff_expiry,
        contract_policy_state=plan.contract_policy_state,
        order_type=ExecutionOrderType.MARKET.value,
        price_policy=plan.price_policy,
        plan_age_seconds=0.0,
        max_plan_age_seconds=plan.max_plan_age_seconds,
    )


def reverse_side(side: OrderSide) -> OrderSide:
    return OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY


def exposure_payload_for_leg(leg: ExecutionLeg, quantity: float) -> dict[str, Any]:
    return {
        "broker": leg.broker.value,
        "symbol": leg.symbol,
        "side": leg.side.value,
        "quantity": quantity,
        "qff_symbol": leg.qff_symbol,
        "qff_expiry": leg.qff_expiry,
        "contract_policy_state": leg.contract_policy_state,
    }


def emergency_event_payload(
    emergency_plan: PairExecutionPlan,
    outcome: ExecutionOutcome,
) -> dict[str, Any]:
    leg = emergency_plan.legs[0]
    return {
        "emergency_plan_id": emergency_plan.plan_id,
        "broker": leg.broker.value,
        "symbol": leg.symbol,
        "side": leg.side.value,
        "quantity": leg.quantity,
        "outcome_status": outcome.status.value,
        "filled_quantity": sum_fills(outcome.fills, leg.broker, leg.symbol),
        "message": outcome.message,
    }


def sum_fills(
    fills: tuple[Fill, ...],
    broker: BrokerName,
    symbol: str,
) -> float:
    return sum(
        fill.quantity
        for fill in fills
        if fill.broker == broker and fill.symbol == symbol
    )
