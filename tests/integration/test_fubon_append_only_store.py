from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from lux_trader.core.models import (
    BrokerName,
    Fill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
)
from lux_trader.store import SQLiteStore


def ts() -> datetime:
    return datetime.fromisoformat("2026-07-16T09:54:02+08:00")


def test_order_and_fill_ids_are_append_only(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "append-only.sqlite3")
    store.initialize()
    try:
        request = OrderRequest(
            broker=BrokerName.FUBON_QFF,
            symbol="QFFH6",
            side=OrderSide.BUY,
            quantity=1,
            price=2444.0,
            timestamp=ts(),
            row_index=1,
        )
        order = OrderResult("LUX-FUBON-attempt-1", request, OrderStatus.FILLED)
        fill = Fill(
            fill_id="FUBON-FILL-LUX-FUBON-attempt-1",
            order_id=order.order_id,
            broker=BrokerName.FUBON_QFF,
            symbol="QFFH6",
            side=OrderSide.BUY,
            quantity=1,
            price=2444.0,
            fee_twd=93.0,
            timestamp=ts(),
            row_index=1,
        )

        store.record_order(order)
        store.record_fill(fill)
        store.record_order(order)
        store.record_fill(fill)

        with pytest.raises(RuntimeError, match="order_id_collision"):
            store.record_order(replace(order, request=replace(request, price=2451.0)))
        with pytest.raises(RuntimeError, match="fill_id_collision"):
            store.record_fill(replace(fill, price=2451.0))
    finally:
        store.close()


def test_fubon_attempt_and_evidence_schema_is_created(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "attempts.sqlite3")
    store.initialize()
    try:
        store.start_fubon_attempt(
            attempt_id="LUX-FUBON-attempt-1",
            plan_id="plan-1",
            created_at=ts(),
            payload={"symbol": "QFFH6"},
        )
        evidence_id = store.record_fubon_evidence(
            attempt_id="LUX-FUBON-attempt-1",
            observed_at=ts(),
            evidence_type="filled_callback",
            payload={"filled_no": "00100012463"},
            accepted=True,
        )
        store.finish_fubon_attempt(
            attempt_id="LUX-FUBON-attempt-1",
            state="filled",
            payload={"confirmation_source": "filled_callback"},
        )
        store.commit()

        assert evidence_id == 1
        row = store.connection.execute(
            "SELECT state FROM fubon_order_attempts WHERE attempt_id = ?",
            ("LUX-FUBON-attempt-1",),
        ).fetchone()
        assert row["state"] == "filled"
    finally:
        store.close()
