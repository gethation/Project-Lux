"""Shared test fakes.

``build_fake_reconciliation_brokers`` was a CLI helper in the legacy project
(behind ``--fake`` flags); the rebuilt CLI only exposes real read-only brokers,
so the fake pair-broker builder lives here and is injected into commands by
monkeypatching ``lux_trader.cli.commands_live.build_reconciliation_brokers``.
"""

from __future__ import annotations

from datetime import datetime

from lux_trader.core.models import BrokerName, Direction, OrderRequest, OrderSide
from lux_trader.execution.intent import (
    ExecutionPlanType,
    pair_execution_plan_from_order_requests,
)
from lux_trader.reconciliation import (
    BrokerPositionSnapshot,
    BrokerReconciler,
    FakeReadOnlyBroker,
)


def reconciliation_qff_symbol(config: object, strategy_state: object) -> str:
    trading_symbol = getattr(strategy_state, "trading_qff_symbol", None)
    return str(trading_symbol or config.live.qff_symbol)


def build_fake_reconciliation_brokers(
    config: object,
    strategy_state: object,
    *,
    fake_case: str,
    timestamp: datetime,
) -> tuple[FakeReadOnlyBroker, FakeReadOnlyBroker]:
    reconciler = BrokerReconciler(
        tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
        qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
    )
    expected = reconciler.expected_from_strategy(
        strategy_state,
        tsm_symbol=config.live.binance_symbol,
        qff_symbol=reconciliation_qff_symbol(config, strategy_state),
        timestamp=timestamp,
    )
    if fake_case == "error":
        return (
            FakeReadOnlyBroker(
                BrokerName.BINANCE_TSM,
                fetch_error=RuntimeError("fake broker fetch failed"),
            ),
            FakeReadOnlyBroker(BrokerName.FUBON_QFF, fetched_at=timestamp),
        )

    tsm_quantity = expected.expected_tsm_units
    qff_quantity = float(expected.expected_qff_contracts)
    if fake_case == "mismatch":
        qff_quantity = qff_quantity + 1.0 if qff_quantity != 0 else 1.0

    tsm_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.BINANCE_TSM,
                symbol=config.live.binance_symbol,
                quantity=tsm_quantity,
            ),
        )
        if tsm_quantity != 0
        else ()
    )
    qff_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.FUBON_QFF,
                symbol=expected.qff_symbol,
                quantity=qff_quantity,
            ),
        )
        if qff_quantity != 0
        else ()
    )
    return (
        FakeReadOnlyBroker(
            BrokerName.BINANCE_TSM,
            account_id="FAKE-BINANCE",
            positions=tsm_positions,
            fetched_at=timestamp,
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON_QFF,
            account_id="FAKE-FUBON",
            positions=qff_positions,
            fetched_at=timestamp,
        ),
    )


def build_fake_execution_plan(
    config: object,
    *,
    fake_case: str,
    timestamp: datetime,
    row_index: int,
):
    qff_symbol = str(config.live.qff_symbol)
    if qff_symbol.lower() == "auto":
        qff_symbol = "QFFG6"
    binance_side = OrderSide.SELL
    if fake_case == "rejected":
        binance_side = OrderSide.BUY
    requests = (
        OrderRequest(
            broker=BrokerName.BINANCE_TSM,
            symbol=config.live.binance_symbol,
            side=binance_side,
            quantity=125.5,
            price=720.0,
            timestamp=timestamp,
            row_index=row_index,
            fee_twd=12.3,
            qff_symbol=qff_symbol,
            qff_expiry="2026-02-18",
            contract_policy_state="fake",
        ),
        OrderRequest(
            broker=BrokerName.FUBON_QFF,
            symbol=qff_symbol,
            side=OrderSide.BUY,
            quantity=3,
            price=1180.0,
            timestamp=timestamp,
            row_index=row_index,
            fee_twd=45.6,
            qff_symbol=qff_symbol,
            qff_expiry="2026-02-18",
            contract_policy_state="fake",
        ),
    )
    return pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_TSM_LONG_QFF,
        requests=requests,
        reason=f"fake_{fake_case}",
        decision_zscore=2.14,
        decision_spread_type="shortSpread",
    )


def make_fake_broker_builder(fake_case: str):
    """Return a drop-in replacement for commands_live.build_reconciliation_brokers."""

    def builder(config, strategy_state, *, readonly):  # noqa: ARG001 - CLI seam
        return build_fake_reconciliation_brokers(
            config,
            strategy_state,
            fake_case=fake_case,
            timestamp=datetime.now().astimezone(),
        )

    return builder
