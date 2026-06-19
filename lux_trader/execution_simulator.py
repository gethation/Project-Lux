from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .execution_intent import ExecutionLeg, PairExecutionPlan
from .models import BrokerName


class ExecutionSimulationScenario(StrEnum):
    LEG_FAILURE = "leg_failure"
    DELAY = "delay"
    CANCEL = "cancel"
    PARTIAL_FILL = "partial_fill"


class ExecutionSimulationStatus(StrEnum):
    SIMULATED_FAILED = "simulated_failed"
    SIMULATED_DELAYED = "simulated_delayed"
    SIMULATED_CANCELED = "simulated_canceled"
    SIMULATED_PARTIAL_FILL = "simulated_partial_fill"


@dataclass(frozen=True)
class ExecutionSimulationResult:
    plan_id: str
    scenario: ExecutionSimulationScenario
    status: ExecutionSimulationStatus
    timestamp: datetime
    broker: BrokerName | None
    symbol: str | None
    message: str
    recommended_state: str = "paused"
    payload: dict[str, Any] | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "scenario": self.scenario.value,
            "status": self.status.value,
            "timestamp": self.timestamp.isoformat(),
            "broker": self.broker.value if self.broker else None,
            "symbol": self.symbol,
            "message": self.message,
            "recommended_state": self.recommended_state,
            "payload": self.payload or {},
        }


class DryRunExecutionSimulator:
    def __init__(self, *, timestamp: datetime | None = None) -> None:
        self.timestamp = timestamp

    def simulate(
        self,
        plan: PairExecutionPlan,
        scenario: ExecutionSimulationScenario | str,
    ) -> ExecutionSimulationResult:
        parsed_scenario = ExecutionSimulationScenario(scenario)
        timestamp = self.timestamp or datetime.now().astimezone()
        target_leg = self._target_leg(plan)
        if parsed_scenario == ExecutionSimulationScenario.LEG_FAILURE:
            return ExecutionSimulationResult(
                plan_id=plan.plan_id,
                scenario=parsed_scenario,
                status=ExecutionSimulationStatus.SIMULATED_FAILED,
                timestamp=timestamp,
                broker=target_leg.broker,
                symbol=target_leg.symbol,
                message="simulated single-leg execution failure",
                payload={
                    "failed_leg": leg_payload(target_leg),
                    "unsubmitted_legs": [
                        leg_payload(leg) for leg in plan.legs if leg != target_leg
                    ],
                },
            )
        if parsed_scenario == ExecutionSimulationScenario.DELAY:
            return ExecutionSimulationResult(
                plan_id=plan.plan_id,
                scenario=parsed_scenario,
                status=ExecutionSimulationStatus.SIMULATED_DELAYED,
                timestamp=timestamp,
                broker=target_leg.broker,
                symbol=target_leg.symbol,
                message="simulated execution delay",
                payload={
                    "delayed_leg": leg_payload(target_leg),
                    "delay_seconds": 30,
                },
            )
        if parsed_scenario == ExecutionSimulationScenario.CANCEL:
            return ExecutionSimulationResult(
                plan_id=plan.plan_id,
                scenario=parsed_scenario,
                status=ExecutionSimulationStatus.SIMULATED_CANCELED,
                timestamp=timestamp,
                broker=None,
                symbol=None,
                message="simulated pair order cancel before execution",
                payload={"canceled_legs": [leg_payload(leg) for leg in plan.legs]},
            )
        partial_leg = target_leg
        return ExecutionSimulationResult(
            plan_id=plan.plan_id,
            scenario=parsed_scenario,
            status=ExecutionSimulationStatus.SIMULATED_PARTIAL_FILL,
            timestamp=timestamp,
            broker=partial_leg.broker,
            symbol=partial_leg.symbol,
            message="simulated partial fill",
            payload={
                "partial_leg": leg_payload(partial_leg),
                "requested_quantity": partial_leg.quantity,
                "filled_quantity": partial_leg.quantity / 2.0,
                "remaining_quantity": partial_leg.quantity / 2.0,
            },
        )

    def _target_leg(self, plan: PairExecutionPlan) -> ExecutionLeg:
        for leg in plan.legs:
            if leg.broker == BrokerName.BINANCE_TSM:
                return leg
        if not plan.legs:
            raise RuntimeError("Cannot simulate execution without execution legs")
        return plan.legs[0]


def leg_payload(leg: ExecutionLeg) -> dict[str, Any]:
    return {
        "broker": leg.broker.value,
        "symbol": leg.symbol,
        "side": leg.side.value,
        "quantity": leg.quantity,
        "price": leg.price,
    }
