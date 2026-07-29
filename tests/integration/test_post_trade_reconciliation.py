from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import (
    BrokerName,
    Direction,
    Fill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from lux_trader.reconciliation.post_trade import PostTradeReconciler
from lux_trader.reconciliation import (
    BrokerPositionSnapshot,
    FakeReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.store import SQLiteStore
from lux_trader.core.strategy import StrategyRuntimeState


SYMBOL_UMC = "UMC"
SYMBOL_CCF = "CCFG6"


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


def order_and_fill(
    *,
    broker: BrokerName,
    symbol: str,
    side: OrderSide,
    quantity: float,
) -> tuple[OrderResult, Fill]:
    request = OrderRequest(
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=100.0,
        timestamp=ts(),
        row_index=7,
    )
    order = OrderResult(
        order_id=f"{broker.value}-{side.value}-{quantity}",
        request=request,
        status=OrderStatus.FILLED,
    )
    fill = Fill(
        fill_id=f"FILL-{broker.value}-{side.value}-{quantity}",
        order_id=order.order_id,
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=100.0,
        fee_twd=0.0,
        timestamp=ts(),
        row_index=7,
    )
    return order, fill


def open_strategy() -> StrategyRuntimeState:
    return StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_UMC_LONG_CCF,
        umc_units=-100.0,
        ccf_contracts=2,
        trading_ccf_symbol=SYMBOL_CCF,
    )


def matching_brokers() -> tuple[FakeReadOnlyBroker, FakeReadOnlyBroker]:
    return (
        FakeReadOnlyBroker(
            BrokerName.IBKR_UMC,
            positions=(
                BrokerPositionSnapshot(
                    broker=BrokerName.IBKR_UMC,
                    symbol=SYMBOL_UMC,
                    quantity=-100.0,
                ),
            ),
            fetched_at=ts(),
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON_CCF,
            positions=(
                BrokerPositionSnapshot(
                    broker=BrokerName.FUBON_CCF,
                    symbol=SYMBOL_CCF,
                    quantity=2.0,
                ),
            ),
            fetched_at=ts(),
        ),
    )


def test_post_trade_reconciliation_matches_broker_and_recorded_fills(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "post_trade.sqlite3")
    try:
        store.initialize()
        for order, fill in (
            order_and_fill(
                broker=BrokerName.IBKR_UMC,
                symbol=SYMBOL_UMC,
                side=OrderSide.SELL,
                quantity=100.0,
            ),
            order_and_fill(
                broker=BrokerName.FUBON_CCF,
                symbol=SYMBOL_CCF,
                side=OrderSide.BUY,
                quantity=2.0,
            ),
        ):
            store.record_order(order)
            store.record_fill(fill)

        report = PostTradeReconciler().reconcile(
            store=store,
            strategy_state=open_strategy(),
            brokers=matching_brokers(),
            umc_symbol=SYMBOL_UMC,
            ccf_symbol=SYMBOL_CCF,
            timestamp=ts(),
        )

        assert report.status == ReconciliationStatus.MATCHED
        assert report.issues == ()
    finally:
        store.close()


def test_post_trade_reconciliation_warns_when_recorded_fills_do_not_match_state(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "post_trade.sqlite3")
    try:
        store.initialize()
        order, fill = order_and_fill(
            broker=BrokerName.IBKR_UMC,
            symbol=SYMBOL_UMC,
            side=OrderSide.SELL,
            quantity=100.0,
        )
        store.record_order(order)
        store.record_fill(fill)

        report = PostTradeReconciler().reconcile(
            store=store,
            strategy_state=open_strategy(),
            brokers=matching_brokers(),
            umc_symbol=SYMBOL_UMC,
            ccf_symbol=SYMBOL_CCF,
            timestamp=ts(),
        )

        assert report.status == ReconciliationStatus.WARNING
        fill_issues = [
            issue
            for issue in report.issues
            if issue.issue_type == "recorded_fill_position_mismatch"
        ]
        assert len(fill_issues) == 1
        assert fill_issues[0].broker == BrokerName.FUBON_CCF
        assert fill_issues[0].expected_quantity == pytest.approx(2.0)
        assert fill_issues[0].actual_quantity == pytest.approx(0.0)
    finally:
        store.close()
