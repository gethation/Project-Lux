from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from ..config import AppConfig
from .intent import PairExecutionPlan
from ..reconciliation import ReconciliationReport, ReconciliationStatus


LIVE_ORDER_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "FUBON_ALLOW_LIVE_ORDER",
    "BINANCE_ALLOW_LIVE_ORDER",
)


class LiveExecutionGateSeverity(StrEnum):
    INFO = "info"
    BLOCKER = "blocker"


@dataclass(frozen=True)
class LiveExecutionGateCheck:
    check_type: str
    passed: bool
    message: str
    severity: LiveExecutionGateSeverity = LiveExecutionGateSeverity.BLOCKER
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class LiveExecutionGateReport:
    timestamp: datetime
    checks: tuple[LiveExecutionGateCheck, ...]

    @property
    def passed(self) -> bool:
        return all(
            check.passed
            for check in self.checks
            if check.severity == LiveExecutionGateSeverity.BLOCKER
        )

    @property
    def failed_checks(self) -> tuple[LiveExecutionGateCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if (
                not check.passed
                and check.severity == LiveExecutionGateSeverity.BLOCKER
            )
        )

    def failure_summary(self) -> str:
        return ", ".join(check.check_type for check in self.failed_checks)


def live_order_env_gates(
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    environ = environ or os.environ
    return {
        name: str(environ.get(name, "")).strip() == "1"
        for name in LIVE_ORDER_ENV_GATES
    }


def evaluate_live_execution_gate(
    config: AppConfig,
    *,
    environ: Mapping[str, str] | None = None,
    reconciliation_report: ReconciliationReport | None = None,
    plan: PairExecutionPlan | None = None,
    plan_has_outcome: bool = False,
    now: datetime | None = None,
    include_reconciliation_checks: bool = True,
    include_plan_checks: bool = True,
) -> LiveExecutionGateReport:
    timestamp = now or datetime.now().astimezone()
    checks: list[LiveExecutionGateCheck] = []

    def add(
        check_type: str,
        passed: bool,
        message: str,
        *,
        severity: LiveExecutionGateSeverity = LiveExecutionGateSeverity.BLOCKER,
        payload: dict[str, Any] | None = None,
    ) -> None:
        checks.append(
            LiveExecutionGateCheck(
                check_type=check_type,
                passed=bool(passed),
                message=message,
                severity=severity,
                payload=payload,
            )
        )

    add(
        "safety_allow_live_order",
        config.safety.allow_live_order,
        "safety.allow_live_order must be true for live-execute",
        payload={"actual": config.safety.allow_live_order},
    )
    add(
        "live_execution_enabled",
        config.live_execution.enabled,
        "live_execution.enabled must be true",
        payload={"actual": config.live_execution.enabled},
    )
    add(
        "execution_order_tw_leg_first",
        config.live_execution.tw_leg_first,
        "first live execution policy requires tw_leg_first=true",
        payload={"actual": config.live_execution.tw_leg_first},
    )

    for name, enabled in live_order_env_gates(environ).items():
        add(
            f"env_{name}",
            enabled,
            f"{name}=1 is required",
            payload={"actual": enabled},
        )

    if include_reconciliation_checks:
        if not config.live_execution.require_readonly_reconciliation:
            add(
                "readonly_reconciliation_required",
                True,
                "readonly reconciliation is disabled by config",
                severity=LiveExecutionGateSeverity.INFO,
            )
        else:
            add(
                "readonly_reconciliation_present",
                reconciliation_report is not None,
                "latest read-only reconciliation report is required",
            )
            if reconciliation_report is not None:
                add(
                    "readonly_reconciliation_matched",
                    reconciliation_report.status == ReconciliationStatus.MATCHED,
                    "latest read-only reconciliation status must be matched",
                    payload={"status": reconciliation_report.status.value},
                )
                issue_types = {
                    issue.issue_type for issue in reconciliation_report.issues
                }
                add(
                    "no_unexpected_positions",
                    "unexpected_position" not in issue_types,
                    "broker snapshots must not contain unexpected positions",
                    payload={"issue_types": sorted(issue_types)},
                )
                add(
                    "no_unexpected_open_orders",
                    "unexpected_open_order" not in issue_types,
                    "broker snapshots must not contain open orders",
                    payload={"issue_types": sorted(issue_types)},
                )

    if include_plan_checks:
        add(
            "execution_plan_present",
            plan is not None,
            "execution plan is required before live order submission",
        )
        if plan is not None:
            age_seconds = (timestamp - plan.timestamp).total_seconds()
            add(
                "execution_plan_fresh",
                0 <= age_seconds <= config.live_execution.max_plan_age_seconds,
                "execution plan age must be within max_plan_age_seconds",
                payload={
                    "age_seconds": age_seconds,
                    "max_plan_age_seconds": (
                        config.live_execution.max_plan_age_seconds
                    ),
                    "plan_timestamp": plan.timestamp.isoformat(),
                },
            )
            add(
                "execution_plan_not_executed",
                not plan_has_outcome,
                "execution plan must not already have an execution outcome",
                payload={"plan_id": plan.plan_id, "has_outcome": plan_has_outcome},
            )

    return LiveExecutionGateReport(timestamp=timestamp, checks=tuple(checks))


def assert_live_execution_gate_open(report: LiveExecutionGateReport) -> None:
    if report.passed:
        return
    raise RuntimeError(f"live-execute gate closed: {report.failure_summary()}")
