from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ..core.models import BrokerName, OrderSide


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
    us_leg_symbol: str
    tw_leg_symbol: str
    expected_us_leg_units: float
    expected_tw_leg_contracts: int


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
                us_leg_symbol=str(expected_payload["us_leg_symbol"]),
                tw_leg_symbol=str(expected_payload["tw_leg_symbol"]),
                expected_us_leg_units=float(expected_payload["expected_us_leg_units"]),
                expected_tw_leg_contracts=int(expected_payload["expected_tw_leg_contracts"]),
            ),
            snapshots=tuple(snapshot_from_jsonable(item) for item in snapshots_payload),
            issues=tuple(issue_from_jsonable(item) for item in issues_payload),
        )


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
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


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
