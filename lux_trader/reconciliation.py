from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from .models import BrokerName, OrderSide
from .strategy import StrategyRuntimeState
from .models import StrategyState


class ReconciliationStatus(StrEnum):
    MATCHED = "matched"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class BrokerPositionSnapshot:
    broker: BrokerName
    symbol: str
    quantity: float
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    broker: BrokerName
    order_id: str
    symbol: str
    side: OrderSide | None
    quantity: float
    status: str
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class BrokerMarginSnapshot:
    broker: BrokerName
    currency: str
    equity: float | None = None
    available: float | None = None
    margin_used: float | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    broker: BrokerName
    account_id: str
    fetched_at: datetime
    positions: tuple[BrokerPositionSnapshot, ...] = ()
    open_orders: tuple[BrokerOrderSnapshot, ...] = ()
    margins: tuple[BrokerMarginSnapshot, ...] = ()
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExpectedBrokerState:
    timestamp: datetime
    tsm_symbol: str
    qff_symbol: str
    expected_tsm_units: float
    expected_qff_contracts: int


@dataclass(frozen=True)
class ReconciliationIssue:
    status: ReconciliationStatus
    issue_type: str
    broker: BrokerName
    symbol: str | None
    message: str
    expected_quantity: float | None = None
    actual_quantity: float | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    timestamp: datetime
    status: ReconciliationStatus
    expected: ExpectedBrokerState
    snapshots: tuple[BrokerAccountSnapshot, ...]
    issues: tuple[ReconciliationIssue, ...]

    def to_jsonable(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any]) -> "ReconciliationReport":
        expected_payload = payload["expected"]
        snapshots_payload = payload.get("snapshots", [])
        issues_payload = payload.get("issues", [])
        return cls(
            timestamp=datetime.fromisoformat(str(payload["timestamp"])),
            status=ReconciliationStatus(payload["status"]),
            expected=ExpectedBrokerState(
                timestamp=datetime.fromisoformat(str(expected_payload["timestamp"])),
                tsm_symbol=str(expected_payload["tsm_symbol"]),
                qff_symbol=str(expected_payload["qff_symbol"]),
                expected_tsm_units=float(expected_payload["expected_tsm_units"]),
                expected_qff_contracts=int(expected_payload["expected_qff_contracts"]),
            ),
            snapshots=tuple(snapshot_from_jsonable(item) for item in snapshots_payload),
            issues=tuple(issue_from_jsonable(item) for item in issues_payload),
        )


class ReadOnlyBroker(Protocol):
    broker: BrokerName

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        ...

    def close(self) -> None:
        ...


class FakeReadOnlyBroker:
    def __init__(
        self,
        broker: BrokerName,
        *,
        account_id: str = "FAKE",
        positions: tuple[BrokerPositionSnapshot, ...] = (),
        open_orders: tuple[BrokerOrderSnapshot, ...] = (),
        margins: tuple[BrokerMarginSnapshot, ...] = (),
        fetch_error: Exception | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        self.broker = broker
        self.account_id = account_id
        self.positions = positions
        self.open_orders = open_orders
        self.margins = margins
        self.fetch_error = fetch_error
        self.fetched_at = fetched_at

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        if self.fetch_error is not None:
            raise self.fetch_error
        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id=self.account_id,
            fetched_at=self.fetched_at or datetime.now().astimezone(),
            positions=self.positions,
            open_orders=self.open_orders,
            margins=self.margins,
            raw={"source": "fake"},
        )

    def close(self) -> None:
        return None


class BrokerReconciler:
    def __init__(
        self,
        *,
        tsm_units_tolerance: float = 1e-6,
        qff_contract_tolerance: int = 0,
    ) -> None:
        self.tsm_units_tolerance = float(tsm_units_tolerance)
        self.qff_contract_tolerance = int(qff_contract_tolerance)

    def reconcile(
        self,
        *,
        strategy_state: StrategyRuntimeState | None,
        brokers: tuple[ReadOnlyBroker, ...],
        tsm_symbol: str,
        qff_symbol: str,
        timestamp: datetime | None = None,
    ) -> ReconciliationReport:
        timestamp = timestamp or datetime.now().astimezone()
        expected = self.expected_from_strategy(
            strategy_state,
            tsm_symbol=tsm_symbol,
            qff_symbol=qff_symbol,
            timestamp=timestamp,
        )
        snapshots: list[BrokerAccountSnapshot] = []
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

        status = report_status(issues)
        return ReconciliationReport(
            timestamp=timestamp,
            status=status,
            expected=expected,
            snapshots=tuple(snapshots),
            issues=tuple(issues),
        )

    def expected_from_strategy(
        self,
        state: StrategyRuntimeState | None,
        *,
        tsm_symbol: str,
        qff_symbol: str,
        timestamp: datetime,
    ) -> ExpectedBrokerState:
        if state is None or state.state not in {
            StrategyState.OPEN,
            StrategyState.EXIT_PENDING,
        }:
            return ExpectedBrokerState(
                timestamp=timestamp,
                tsm_symbol=tsm_symbol,
                qff_symbol=qff_symbol,
                expected_tsm_units=0.0,
                expected_qff_contracts=0,
            )
        return ExpectedBrokerState(
            timestamp=timestamp,
            tsm_symbol=tsm_symbol,
            qff_symbol=state.trading_qff_symbol or qff_symbol,
            expected_tsm_units=state.tsm_units,
            expected_qff_contracts=state.qff_contracts,
        )

    def _snapshot_issues(
        self,
        snapshot: BrokerAccountSnapshot,
        expected: ExpectedBrokerState,
    ) -> list[ReconciliationIssue]:
        issues: list[ReconciliationIssue] = []
        expected_symbol = (
            expected.tsm_symbol
            if snapshot.broker == BrokerName.BINANCE_TSM
            else expected.qff_symbol
        )
        expected_quantity = (
            expected.expected_tsm_units
            if snapshot.broker == BrokerName.BINANCE_TSM
            else float(expected.expected_qff_contracts)
        )
        tolerance = (
            self.tsm_units_tolerance
            if snapshot.broker == BrokerName.BINANCE_TSM
            else float(self.qff_contract_tolerance)
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


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def snapshot_from_jsonable(payload: dict[str, Any]) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker=BrokerName(payload["broker"]),
        account_id=str(payload["account_id"]),
        fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
        positions=tuple(
            BrokerPositionSnapshot(
                broker=BrokerName(item["broker"]),
                symbol=str(item["symbol"]),
                quantity=float(item["quantity"]),
                raw=item.get("raw"),
            )
            for item in payload.get("positions", [])
        ),
        open_orders=tuple(
            BrokerOrderSnapshot(
                broker=BrokerName(item["broker"]),
                order_id=str(item["order_id"]),
                symbol=str(item["symbol"]),
                side=OrderSide(item["side"]) if item.get("side") else None,
                quantity=float(item["quantity"]),
                status=str(item["status"]),
                raw=item.get("raw"),
            )
            for item in payload.get("open_orders", [])
        ),
        margins=tuple(
            BrokerMarginSnapshot(
                broker=BrokerName(item["broker"]),
                currency=str(item["currency"]),
                equity=item.get("equity"),
                available=item.get("available"),
                margin_used=item.get("margin_used"),
                raw=item.get("raw"),
            )
            for item in payload.get("margins", [])
        ),
        raw=payload.get("raw"),
    )


def issue_from_jsonable(payload: dict[str, Any]) -> ReconciliationIssue:
    return ReconciliationIssue(
        status=ReconciliationStatus(payload["status"]),
        issue_type=str(payload["issue_type"]),
        broker=BrokerName(payload["broker"]),
        symbol=payload.get("symbol"),
        message=str(payload["message"]),
        expected_quantity=payload.get("expected_quantity"),
        actual_quantity=payload.get("actual_quantity"),
        payload=payload.get("payload"),
    )
