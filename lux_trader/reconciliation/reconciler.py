from __future__ import annotations

from datetime import datetime

from ..core.models import BrokerName
from ..core.models import StrategyState
from ..core.strategy import StrategyRuntimeState
from .brokers import ReadOnlyBroker
from .models import (
    BrokerAccountSnapshot,
    ExpectedBrokerState,
    ReconciliationIssue,
    ReconciliationReport,
    ReconciliationStatus,
)


class BrokerReconciler:
    def __init__(
        self,
        *,
        us_leg_units_tolerance: float = 1e-6,
        tw_leg_contract_tolerance: int = 0,
    ) -> None:
        self.us_leg_units_tolerance = float(us_leg_units_tolerance)
        self.tw_leg_contract_tolerance = int(tw_leg_contract_tolerance)

    def reconcile(
        self,
        *,
        strategy_state: StrategyRuntimeState | None,
        brokers: tuple[ReadOnlyBroker, ...],
        us_leg_symbol: str,
        tw_leg_symbol: str,
        timestamp: datetime | None = None,
    ) -> ReconciliationReport:
        timestamp = timestamp or datetime.now().astimezone()
        expected = self.expected_from_strategy(
            strategy_state,
            us_leg_symbol=us_leg_symbol,
            tw_leg_symbol=tw_leg_symbol,
            timestamp=timestamp,
        )
        snapshots = []
        issues: list[ReconciliationIssue] = []

        for broker in brokers:
            try:
                snapshot = broker.fetch_snapshot()
            except Exception as exc:
                issues.append(
                    ReconciliationIssue(
                        status=ReconciliationStatus.ERROR,
                        issue_type="broker_fetch_failed",
                        broker=broker.broker,
                        symbol=None,
                        message=f"{broker.broker.value} snapshot fetch failed: {exc}",
                        payload={"error_type": type(exc).__name__, "error": str(exc)},
                    )
                )
                continue
            snapshots.append(snapshot)
            issues.extend(self._snapshot_issues(snapshot, expected))

        return ReconciliationReport(
            timestamp=timestamp,
            status=report_status(issues),
            expected=expected,
            snapshots=tuple(snapshots),
            issues=tuple(issues),
        )

    def expected_from_strategy(
        self,
        state: StrategyRuntimeState | None,
        *,
        us_leg_symbol: str,
        tw_leg_symbol: str,
        timestamp: datetime,
    ) -> ExpectedBrokerState:
        has_position = (
            state is not None
            and state.position_direction is not None
            and (
                abs(float(state.us_leg_units)) > self.us_leg_units_tolerance
                or abs(float(state.tw_leg_contracts)) > self.tw_leg_contract_tolerance
            )
        )
        if state is None or not (
            state.state in {StrategyState.OPEN, StrategyState.EXIT_PENDING}
            or has_position
        ):
            return ExpectedBrokerState(
                timestamp=timestamp,
                us_leg_symbol=us_leg_symbol,
                tw_leg_symbol=tw_leg_symbol,
                expected_us_leg_units=0.0,
                expected_tw_leg_contracts=0,
            )
        return ExpectedBrokerState(
            timestamp=timestamp,
            us_leg_symbol=us_leg_symbol,
            tw_leg_symbol=state.trading_tw_leg_symbol or tw_leg_symbol,
            expected_us_leg_units=state.us_leg_units,
            expected_tw_leg_contracts=state.tw_leg_contracts,
        )

    def _snapshot_issues(
        self,
        snapshot: BrokerAccountSnapshot,
        expected: ExpectedBrokerState,
    ) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        expected_symbol = (
            expected.us_leg_symbol
            if snapshot.broker == BrokerName.BINANCE
            else expected.tw_leg_symbol
        )
        expected_quantity = (
            expected.expected_us_leg_units
            if snapshot.broker == BrokerName.BINANCE
            else float(expected.expected_tw_leg_contracts)
        )
        tolerance = (
            self.us_leg_units_tolerance
            if snapshot.broker == BrokerName.BINANCE
            else float(self.tw_leg_contract_tolerance)
        )
        actual_quantity = sum(
            position.quantity
            for position in snapshot.positions
            if position.symbol == expected_symbol
        )
        if abs(actual_quantity - expected_quantity) > tolerance:
            issue_type = (
                "unexpected_position"
                if abs(expected_quantity) <= tolerance
                else "position_quantity_mismatch"
            )
            issues.append(
                ReconciliationIssue(
                    status=ReconciliationStatus.WARNING,
                    issue_type=issue_type,
                    broker=snapshot.broker,
                    symbol=expected_symbol,
                    message=(
                        f"{snapshot.broker.value} {expected_symbol} "
                        f"expected={expected_quantity} actual={actual_quantity}"
                    ),
                    expected_quantity=expected_quantity,
                    actual_quantity=actual_quantity,
                )
            )

        for position in snapshot.positions:
            if position.symbol == expected_symbol:
                continue
            if abs(position.quantity) > tolerance:
                issues.append(
                    ReconciliationIssue(
                        status=ReconciliationStatus.WARNING,
                        issue_type="unexpected_position",
                        broker=snapshot.broker,
                        symbol=position.symbol,
                        message=(
                            f"{snapshot.broker.value} unexpected position "
                            f"{position.symbol} actual={position.quantity}"
                        ),
                        expected_quantity=0.0,
                        actual_quantity=position.quantity,
                    )
                )

        for order in snapshot.open_orders:
            issues.append(
                ReconciliationIssue(
                    status=ReconciliationStatus.WARNING,
                    issue_type="unexpected_open_order",
                    broker=snapshot.broker,
                    symbol=order.symbol,
                    message=(
                        f"{snapshot.broker.value} has open order "
                        f"{order.order_id} {order.symbol}"
                    ),
                    actual_quantity=order.quantity,
                    payload={"order_id": order.order_id, "status": order.status},
                )
            )
        return issues


def report_status(issues: list[ReconciliationIssue]) -> ReconciliationStatus:
    if any(issue.status == ReconciliationStatus.ERROR for issue in issues):
        return ReconciliationStatus.ERROR
    if issues:
        return ReconciliationStatus.WARNING
    return ReconciliationStatus.MATCHED
