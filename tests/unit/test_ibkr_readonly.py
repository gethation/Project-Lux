from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from lux_trader.core.models import BrokerName, OrderSide
from lux_trader.integrations.env import READONLY_BROKER_ENV
from lux_trader.integrations.ibkr.readonly import IbkrReadOnlyBroker
from lux_trader.reconciliation.brokers import ReadOnlyBroker


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.closed = False

    def fetch_account_snapshot(self) -> dict[str, Any]:
        return self.payload

    def close(self) -> None:
        self.closed = True


def snapshot_payload() -> dict[str, Any]:
    return {
        "accounts": ["U1234567"],
        "positions": [
            {
                "account": "U1234567",
                "symbol": "UMC",
                "sec_type": "STK",
                "currency": "USD",
                "con_id": 46613372,
                "quantity": 460.0,
                "avg_cost": 21.3,
            }
        ],
        "open_orders": [
            {
                "order_id": 12,
                "symbol": "UMC",
                "action": "BUY",
                "quantity": 100.0,
                "status": "PreSubmitted",
            }
        ],
        "account_values": [
            {"account": "U1234567", "tag": "NetLiquidation",
             "value": "10150.00", "currency": "USD"},
            {"account": "U1234567", "tag": "AvailableFunds",
             "value": "9800.50", "currency": "USD"},
            {"account": "U1234567", "tag": "MaintMarginReq",
             "value": "349.50", "currency": "USD"},
        ],
        "fetched_at": datetime(2026, 7, 24, 9, 30),
    }


@pytest.fixture
def readonly_env(monkeypatch) -> None:
    monkeypatch.setenv(READONLY_BROKER_ENV, "1")


def test_satisfies_the_readonly_broker_protocol(readonly_env) -> None:
    # ReadOnlyBroker is not @runtime_checkable, so check the shape structurally
    broker = IbkrReadOnlyBroker(FakeClient(snapshot_payload()))
    for member in ReadOnlyBroker.__protocol_attrs__:
        assert hasattr(broker, member), f"missing protocol member: {member}"
    assert broker.broker is BrokerName.IBKR


def test_env_gate_blocks_construction_when_unset(monkeypatch) -> None:
    monkeypatch.delenv(READONLY_BROKER_ENV, raising=False)
    with pytest.raises(RuntimeError, match=READONLY_BROKER_ENV):
        IbkrReadOnlyBroker(FakeClient(snapshot_payload()))


def test_snapshot_maps_positions_orders_and_margins(readonly_env) -> None:
    broker = IbkrReadOnlyBroker(FakeClient(snapshot_payload()))
    snapshot = broker.fetch_snapshot()

    assert snapshot.broker is BrokerName.IBKR
    assert snapshot.account_id == "U1234567"
    assert str(snapshot.fetched_at.tzinfo) == "Asia/Taipei"

    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].symbol == "UMC"
    assert snapshot.positions[0].quantity == pytest.approx(460.0)

    assert len(snapshot.open_orders) == 1
    assert snapshot.open_orders[0].side is OrderSide.BUY
    assert snapshot.open_orders[0].order_id == "12"
    assert snapshot.open_orders[0].status == "PreSubmitted"

    assert len(snapshot.margins) == 1
    margin = snapshot.margins[0]
    assert margin.currency == "USD"
    assert margin.equity == pytest.approx(10_150.00)
    assert margin.available == pytest.approx(9_800.50)
    assert margin.margin_used == pytest.approx(349.50)


def test_snapshot_survives_an_empty_account(readonly_env) -> None:
    payload = snapshot_payload()
    payload.update(accounts=[], positions=[], open_orders=[], account_values=[])
    snapshot = IbkrReadOnlyBroker(FakeClient(payload)).fetch_snapshot()

    assert snapshot.account_id == ""
    assert snapshot.positions == ()
    assert snapshot.open_orders == ()
    assert snapshot.margins[0].equity is None


def test_unknown_order_action_becomes_none_rather_than_guessing(readonly_env) -> None:
    payload = snapshot_payload()
    payload["open_orders"][0]["action"] = "SSHORT"
    snapshot = IbkrReadOnlyBroker(FakeClient(payload)).fetch_snapshot()
    assert snapshot.open_orders[0].side is None


def test_does_not_close_a_client_it_does_not_own(readonly_env) -> None:
    client = FakeClient(snapshot_payload())
    IbkrReadOnlyBroker(client).close()
    assert client.closed is False


def test_exposes_no_order_placing_method(readonly_env) -> None:
    broker = IbkrReadOnlyBroker(FakeClient(snapshot_payload()))
    for forbidden in ("place_order", "placeOrder", "execute", "cancel_order"):
        assert not hasattr(broker, forbidden)
