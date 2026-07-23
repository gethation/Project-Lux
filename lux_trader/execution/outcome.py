from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .intent import ExecutionLeg, PairExecutionPlan
from .recorder import DryRunExecutionRecorder
from ..core.models import (
    Fill,
    OrderRequest,
    OrderResult,
    OrderStatus,
    StrategyState,
    dataclass_to_jsonable,
)


class ExecutionOutcomeStatus(StrEnum):
    FILLED = "filled"
    REJECTED = "rejected"
    FAILED = "failed"
    PARTIAL_FILL = "partial_fill"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExecutionOutcome:
    plan_id: str
    timestamp: datetime
    status: ExecutionOutcomeStatus
    message: str
    orders: tuple[OrderResult, ...] = ()
    fills: tuple[Fill, ...] = ()
    recommended_state: StrategyState | None = None
    payload: dict[str, Any] | None = None

    @property
    def filled(self) -> bool:
        return self.status == ExecutionOutcomeStatus.FILLED

    def to_jsonable(self) -> dict[str, Any]:
        return dataclass_to_jsonable(self)


@dataclass(frozen=True)
class ExecutionPreflight:
    open_orders: tuple[dict[str, Any], ...]
    position_quantity: float


class PlanExecutor(Protocol):
    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        ...

    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]:
        ...

    def fetch_position_quantity(self) -> float:
        ...

    def preflight(self) -> ExecutionPreflight:
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class OrderRecordsProvider(Protocol):
    def fetch_order_records(self) -> tuple[dict[str, Any], ...]:
        ...


@runtime_checkable
class SessionHealthProvider(Protocol):
    def session_health(self) -> dict[str, Any]:
        ...


class SimulatedExecutionAdapter:
    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        orders: list[OrderResult] = []
        fills: list[Fill] = []
        for index, leg in enumerate(plan.legs, start=1):
            request = order_request_from_execution_leg(leg)
            order_id = f"DRYRUN-{plan.plan_id}-{index:02d}"
            fill_id = f"DRYRUN-FILL-{plan.plan_id}-{index:02d}"
            order = OrderResult(
                order_id=order_id,
                request=request,
                status=OrderStatus.FILLED,
            )
            fill = Fill(
                fill_id=fill_id,
                order_id=order_id,
                broker=leg.broker,
                symbol=leg.symbol,
                side=leg.side,
                quantity=leg.quantity,
                price=leg.expected_price or leg.price,
                fee_twd=leg.fee_twd,
                timestamp=leg.timestamp,
                row_index=leg.row_index,
                tw_leg_symbol=leg.tw_leg_symbol,
                tw_leg_expiry=leg.tw_leg_expiry,
                contract_policy_state=leg.contract_policy_state,
            )
            orders.append(order)
            fills.append(fill)
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=ExecutionOutcomeStatus.FILLED,
            message="simulated execution filled",
            orders=tuple(orders),
            fills=tuple(fills),
            payload={"adapter": "simulated"},
        )


class ExecutionCoordinator:
    def __init__(
        self,
        store: Any,
        recorder: DryRunExecutionRecorder,
        adapter: PlanExecutor,
    ) -> None:
        self.store = store
        self.recorder = recorder
        self.adapter = adapter

    def execute(
        self,
        plan: PairExecutionPlan,
    ) -> tuple[PairExecutionPlan, ExecutionOutcome]:
        recorded = self.recorder.record_plan(plan)
        if not recorded.accepted:
            outcome = ExecutionOutcome(
                plan_id=recorded.plan_id,
                timestamp=recorded.timestamp,
                status=ExecutionOutcomeStatus.REJECTED,
                message="execution plan rejected",
                recommended_state=StrategyState.PAUSED,
                payload={
                    "failed_checks": sum(
                        1 for check in recorded.checks if not check.passed
                    )
                },
            )
        else:
            try:
                outcome = self.adapter.execute(recorded)
            except Exception as exc:
                outcome = ExecutionOutcome(
                    plan_id=recorded.plan_id,
                    timestamp=recorded.timestamp,
                    status=ExecutionOutcomeStatus.FAILED,
                    message=f"{type(exc).__name__}: {exc}",
                    recommended_state=StrategyState.PAUSED,
                    payload={"adapter_error": type(exc).__name__},
                )
        self.store.record_execution_outcome(outcome)
        return recorded, outcome


def order_request_from_execution_leg(leg: ExecutionLeg) -> OrderRequest:
    return OrderRequest(
        broker=leg.broker,
        symbol=leg.symbol,
        side=leg.side,
        quantity=leg.quantity,
        price=leg.price,
        timestamp=leg.timestamp,
        row_index=leg.row_index,
        fee_twd=leg.fee_twd,
        tw_leg_symbol=leg.tw_leg_symbol,
        tw_leg_expiry=leg.tw_leg_expiry,
        contract_policy_state=leg.contract_policy_state,
        order_type=leg.order_type,
        expected_price=leg.expected_price,
        trigger_bid=leg.trigger_bid,
        trigger_ask=leg.trigger_ask,
        trigger_mid=leg.trigger_mid,
        price_source=leg.price_source,
    )
