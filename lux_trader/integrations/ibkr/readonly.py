from __future__ import annotations

from typing import Any

from ...core.models import BrokerName, OrderSide
from ...core.time import ensure_taipei
from ...reconciliation.models import (
    BrokerAccountSnapshot,
    BrokerMarginSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
)
from ..env import require_readonly_broker_env
from .client_process import IbkrClientProcess


# Account-summary tags worth carrying into the margin snapshot.
EQUITY_TAG = "NetLiquidation"
AVAILABLE_TAG = "AvailableFunds"
MAINTENANCE_TAG = "MaintMarginReq"


def _order_side(action: Any) -> OrderSide | None:
    text = str(action or "").strip().upper()
    if text == "BUY":
        return OrderSide.BUY
    if text == "SELL":
        return OrderSide.SELL
    return None


def _tag_value(values: list[dict[str, Any]], tag: str) -> tuple[float | None, str]:
    for row in values:
        if row.get("tag") == tag:
            try:
                return float(row["value"]), str(row.get("currency") or "USD")
            except (TypeError, ValueError):
                return None, str(row.get("currency") or "USD")
    return None, "USD"


class IbkrReadOnlyBroker:
    """Read-only IBKR account view implementing the ReadOnlyBroker protocol.

    Deliberately has no order-placing capability of any kind. It is gated behind
    the same LUX_READONLY_BROKER environment variable the Fubon and Binance
    read-only brokers use, so one switch governs every venue.
    """

    broker = BrokerName.IBKR

    def __init__(
        self,
        client: IbkrClientProcess | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 17_003,
        require_env_gate: bool = True,
    ) -> None:
        if require_env_gate:
            require_readonly_broker_env()
        self.client = client or IbkrClientProcess(
            host=host, port=port, client_id=client_id
        )
        self._owns_client = client is None

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        payload = self.client.fetch_account_snapshot()
        accounts = [str(account) for account in payload.get("accounts", [])]
        values = [dict(row) for row in payload.get("account_values", [])]

        positions = tuple(
            BrokerPositionSnapshot(
                broker=self.broker,
                symbol=str(row.get("symbol") or ""),
                quantity=float(row.get("quantity") or 0.0),
                raw=dict(row),
            )
            for row in payload.get("positions", [])
        )
        open_orders = tuple(
            BrokerOrderSnapshot(
                broker=self.broker,
                order_id=str(row.get("order_id") or ""),
                symbol=str(row.get("symbol") or ""),
                side=_order_side(row.get("action")),
                quantity=float(row.get("quantity") or 0.0),
                status=str(row.get("status") or ""),
                raw=dict(row),
            )
            for row in payload.get("open_orders", [])
        )

        equity, currency = _tag_value(values, EQUITY_TAG)
        available, _ = _tag_value(values, AVAILABLE_TAG)
        maintenance, _ = _tag_value(values, MAINTENANCE_TAG)
        margins = (
            BrokerMarginSnapshot(
                broker=self.broker,
                currency=currency,
                equity=equity,
                available=available,
                margin_used=maintenance,
                raw={"account_values": values},
            ),
        )

        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id=accounts[0] if accounts else "",
            fetched_at=ensure_taipei(payload["fetched_at"]),
            positions=positions,
            open_orders=open_orders,
            margins=margins,
            raw=dict(payload),
        )

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "IbkrReadOnlyBroker":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


__all__ = ["IbkrReadOnlyBroker"]
