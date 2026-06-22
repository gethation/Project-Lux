from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import BrokerName
from .reconciliation import (
    BrokerReconciler,
    ExpectedBrokerState,
    ReadOnlyBroker,
    ReconciliationIssue,
    ReconciliationReport,
    ReconciliationStatus,
    report_status,
)
from .strategy import StrategyRuntimeState


class PostTradeReconciler:
    def __init__(
        self,
        *,
        tsm_units_tolerance: float = 1e-6,
        qff_contract_tolerance: int = 0,
    ) -> None:
        self.tsm_units_tolerance = float(tsm_units_tolerance)
        self.qff_contract_tolerance = int(qff_contract_tolerance)
        self.broker_reconciler = BrokerReconciler(
            tsm_units_tolerance=tsm_units_tolerance,
            qff_contract_tolerance=qff_contract_tolerance,
        )

    def reconcile(
        self,
        *,
        store: Any,
        strategy_state: StrategyRuntimeState,
        brokers: tuple[ReadOnlyBroker, ...],
        tsm_symbol: str,
        qff_symbol: str,
        timestamp: datetime,
    ) -> ReconciliationReport:
        report = self.broker_reconciler.reconcile(
            strategy_state=strategy_state,
            brokers=brokers,
            tsm_symbol=tsm_symbol,
            qff_symbol=qff_symbol,
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
            tsm_symbol=expected.tsm_symbol,
            qff_symbol=expected.qff_symbol,
        )
        checks = (
            (
                BrokerName.BINANCE_TSM,
                expected.tsm_symbol,
                expected.expected_tsm_units,
                self.tsm_units_tolerance,
            ),
            (
                BrokerName.FUBON_QFF,
                expected.qff_symbol,
                float(expected.expected_qff_contracts),
                float(self.qff_contract_tolerance),
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
