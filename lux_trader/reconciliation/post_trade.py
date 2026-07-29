from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.models import BrokerName
from ..core.strategy import StrategyRuntimeState
from .brokers import ReadOnlyBroker
from .models import (
    ExpectedBrokerState,
    ReconciliationIssue,
    ReconciliationReport,
    ReconciliationStatus,
)
from .reconciler import (
    BrokerReconciler,
    report_status,
)


class PostTradeReconciler:
    def __init__(
        self,
        *,
        umc_units_tolerance: float = 1e-6,
        ccf_contract_tolerance: int = 0,
    ) -> None:
        self.umc_units_tolerance = float(umc_units_tolerance)
        self.ccf_contract_tolerance = int(ccf_contract_tolerance)
        self.broker_reconciler = BrokerReconciler(
            umc_units_tolerance=umc_units_tolerance,
            ccf_contract_tolerance=ccf_contract_tolerance,
        )

    def reconcile(
        self,
        *,
        store: Any,
        strategy_state: StrategyRuntimeState,
        brokers: tuple[ReadOnlyBroker, ...],
        umc_symbol: str,
        ccf_symbol: str,
        timestamp: datetime,
    ) -> ReconciliationReport:
        report = self.broker_reconciler.reconcile(
            strategy_state=strategy_state,
            brokers=brokers,
            umc_symbol=umc_symbol,
            ccf_symbol=ccf_symbol,
            timestamp=timestamp,
        )
        issues = list(report.issues)
        issues.extend(
            self._recorded_fill_issues(
                store,
                report.expected,
            )
        )
        return ReconciliationReport(
            timestamp=report.timestamp,
            status=report_status(issues),
            expected=report.expected,
            snapshots=report.snapshots,
            issues=tuple(issues),
        )

    def _recorded_fill_issues(
        self,
        store: Any,
        expected: ExpectedBrokerState,
    ) -> list[ReconciliationIssue]:
        exposure = store.load_recorded_fill_exposure(
            umc_symbol=expected.umc_symbol,
            ccf_symbol=expected.ccf_symbol,
        )
        checks = (
            (
                BrokerName.IBKR_UMC,
                expected.umc_symbol,
                expected.expected_umc_units,
                self.umc_units_tolerance,
            ),
            (
                BrokerName.FUBON_CCF,
                expected.ccf_symbol,
                float(expected.expected_ccf_contracts),
                float(self.ccf_contract_tolerance),
            ),
        )
        issues: list[ReconciliationIssue] = []
        for broker, symbol, expected_quantity, tolerance in checks:
            actual_quantity = float(exposure.get(broker, 0.0))
            if abs(actual_quantity - expected_quantity) <= tolerance:
                continue
            issues.append(
                ReconciliationIssue(
                    status=ReconciliationStatus.WARNING,
                    issue_type="recorded_fill_position_mismatch",
                    broker=broker,
                    symbol=symbol,
                    message=(
                        f"recorded fills {broker.value} {symbol} "
                        f"expected={expected_quantity} actual={actual_quantity}"
                    ),
                    expected_quantity=expected_quantity,
                    actual_quantity=actual_quantity,
                    payload={
                        "source": "fills",
                        "tolerance": tolerance,
                    },
                )
            )
        return issues
