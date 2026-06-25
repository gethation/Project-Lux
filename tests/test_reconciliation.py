from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import BrokerName, Direction, OrderSide, StrategyState
from lux_trader.reconciliation import (
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
    BrokerReconciler,
    FakeReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.core.strategy import StrategyRuntimeState


def ts(value: str = "2026-06-18T09:00:00+08:00") -> datetime:
    return datetime.fromisoformat(value)


def position(
    broker: BrokerName,
    symbol: str,
    quantity: float,
) -> BrokerPositionSnapshot:
    return BrokerPositionSnapshot(broker=broker, symbol=symbol, quantity=quantity)


def open_order(
    broker: BrokerName,
    symbol: str,
    quantity: float,
) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        broker=broker,
        order_id=f"{broker.value}-1",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        status="open",
    )


def reconcile(
    state: StrategyRuntimeState | None,
    *brokers: FakeReadOnlyBroker,
):
    return BrokerReconciler().reconcile(
        strategy_state=state,
        brokers=brokers,
        tsm_symbol="TSM/USDT:USDT",
        qff_symbol="QFFG6",
        timestamp=ts(),
    )


def test_flat_state_with_zero_broker_positions_matches() -> None:
    report = reconcile(
        StrategyRuntimeState(state=StrategyState.FLAT),
        FakeReadOnlyBroker(BrokerName.BINANCE_TSM, fetched_at=ts()),
        FakeReadOnlyBroker(BrokerName.FUBON_QFF, fetched_at=ts()),
    )

    assert report.status == ReconciliationStatus.MATCHED
    assert report.issues == ()
    assert report.expected.expected_tsm_units == 0.0
    assert report.expected.expected_qff_contracts == 0


def test_flat_state_with_nonzero_broker_position_warns() -> None:
    report = reconcile(
        StrategyRuntimeState(state=StrategyState.FLAT),
        FakeReadOnlyBroker(
            BrokerName.BINANCE_TSM,
            positions=(position(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", 12.0),),
            fetched_at=ts(),
        ),
    )

    assert report.status == ReconciliationStatus.WARNING
    assert len(report.issues) == 1
    assert report.issues[0].issue_type == "unexpected_position"
    assert report.issues[0].actual_quantity == pytest.approx(12.0)


def test_open_short_position_matches_signed_broker_quantities() -> None:
    state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
        tsm_units=-2150.5,
        qff_contracts=4,
        trading_qff_symbol="QFFG6",
    )

    report = reconcile(
        state,
        FakeReadOnlyBroker(
            BrokerName.BINANCE_TSM,
            positions=(position(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", -2150.5),),
            fetched_at=ts(),
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON_QFF,
            positions=(position(BrokerName.FUBON_QFF, "QFFG6", 4),),
            fetched_at=ts(),
        ),
    )

    assert report.status == ReconciliationStatus.MATCHED
    assert report.issues == ()


def test_tsm_quantity_mismatch_warns_when_over_tolerance() -> None:
    state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
        tsm_units=-100.0,
        qff_contracts=1,
        trading_qff_symbol="QFFG6",
    )

    report = BrokerReconciler(tsm_units_tolerance=1e-6).reconcile(
        strategy_state=state,
        brokers=(
            FakeReadOnlyBroker(
                BrokerName.BINANCE_TSM,
                positions=(position(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", -99.0),),
                fetched_at=ts(),
            ),
        ),
        tsm_symbol="TSM/USDT:USDT",
        qff_symbol="QFFG6",
        timestamp=ts(),
    )

    assert report.status == ReconciliationStatus.WARNING
    assert report.issues[0].issue_type == "position_quantity_mismatch"
    assert report.issues[0].expected_quantity == pytest.approx(-100.0)
    assert report.issues[0].actual_quantity == pytest.approx(-99.0)


def test_qff_contract_mismatch_warns() -> None:
    state = StrategyRuntimeState(
        state=StrategyState.EXIT_PENDING,
        position_direction=Direction.LONG_TSM_SHORT_QFF,
        tsm_units=100.0,
        qff_contracts=-2,
        trading_qff_symbol="QFFG6",
    )

    report = reconcile(
        state,
        FakeReadOnlyBroker(
            BrokerName.FUBON_QFF,
            positions=(position(BrokerName.FUBON_QFF, "QFFG6", -1),),
            fetched_at=ts(),
        ),
    )

    assert report.status == ReconciliationStatus.WARNING
    assert report.issues[0].issue_type == "position_quantity_mismatch"
    assert report.issues[0].expected_quantity == pytest.approx(-2.0)
    assert report.issues[0].actual_quantity == pytest.approx(-1.0)


def test_open_order_warns_even_when_position_matches() -> None:
    report = reconcile(
        StrategyRuntimeState(state=StrategyState.FLAT),
        FakeReadOnlyBroker(
            BrokerName.FUBON_QFF,
            open_orders=(open_order(BrokerName.FUBON_QFF, "QFFG6", 1),),
            fetched_at=ts(),
        ),
    )

    assert report.status == ReconciliationStatus.WARNING
    assert report.issues[0].issue_type == "unexpected_open_order"
    assert report.issues[0].symbol == "QFFG6"


def test_broker_fetch_error_marks_report_error() -> None:
    report = reconcile(
        StrategyRuntimeState(state=StrategyState.FLAT),
        FakeReadOnlyBroker(
            BrokerName.BINANCE_TSM,
            fetch_error=RuntimeError("private api unavailable"),
        ),
    )

    assert report.status == ReconciliationStatus.ERROR
    assert report.issues[0].issue_type == "broker_fetch_failed"
    assert report.issues[0].status == ReconciliationStatus.ERROR


def test_paused_state_with_position_still_expects_open_exposure() -> None:
    state = StrategyRuntimeState(
        state=StrategyState.PAUSED,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
        tsm_units=-2150.5,
        qff_contracts=4,
        trading_qff_symbol="QFFG6",
    )

    report = reconcile(
        state,
        FakeReadOnlyBroker(
            BrokerName.BINANCE_TSM,
            positions=(position(BrokerName.BINANCE_TSM, "TSM/USDT:USDT", -2150.5),),
            fetched_at=ts(),
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON_QFF,
            positions=(position(BrokerName.FUBON_QFF, "QFFG6", 4),),
            fetched_at=ts(),
        ),
    )

    assert report.status == ReconciliationStatus.MATCHED
    assert report.expected.expected_tsm_units == pytest.approx(-2150.5)
    assert report.expected.expected_qff_contracts == 4
