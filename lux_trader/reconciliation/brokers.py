from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..core.models import BrokerName
from .models import (
    BrokerAccountSnapshot,
    BrokerMarginSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
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
