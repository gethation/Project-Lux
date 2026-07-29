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


def reconciliation_ccf_symbol(config: object, strategy_state: object) -> str:
    trading_symbol = getattr(strategy_state, "trading_ccf_symbol", None)
    return str(trading_symbol or config.live.ccf_symbol)


def build_fake_reconciliation_brokers(
    config: object,
    strategy_state: object,
    *,
    fake_case: str,
    timestamp: datetime,
) -> tuple[FakeReadOnlyBroker, FakeReadOnlyBroker]:
    reconciler = BrokerReconciler(
        umc_units_tolerance=config.broker_reconciliation.umc_units_tolerance,
        ccf_contract_tolerance=config.broker_reconciliation.ccf_contract_tolerance,
    )
    expected = reconciler.expected_from_strategy(
        strategy_state,
        umc_symbol=config.live.binance_symbol,
        ccf_symbol=reconciliation_ccf_symbol(config, strategy_state),
        timestamp=timestamp,
    )
    if fake_case == "error":
        return (
            FakeReadOnlyBroker(
                BrokerName.IBKR_UMC,
                fetch_error=RuntimeError("fake broker fetch failed"),
            ),
            FakeReadOnlyBroker(BrokerName.FUBON_CCF, fetched_at=timestamp),
        )

    umc_quantity = expected.expected_umc_units
    ccf_quantity = float(expected.expected_ccf_contracts)
    if fake_case == "mismatch":
        ccf_quantity = ccf_quantity + 1.0 if ccf_quantity != 0 else 1.0

    umc_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.IBKR_UMC,
                symbol=config.live.binance_symbol,
                quantity=umc_quantity,
            ),
        )
        if umc_quantity != 0
        else ()
    )
    ccf_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.FUBON_CCF,
                symbol=expected.ccf_symbol,
                quantity=ccf_quantity,
            ),
        )
        if ccf_quantity != 0
        else ()
    )
    return (
        FakeReadOnlyBroker(
            BrokerName.IBKR_UMC,
            account_id="FAKE-BINANCE",
            positions=umc_positions,
            fetched_at=timestamp,
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON_CCF,
            account_id="FAKE-FUBON",
            positions=ccf_positions,
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
    ccf_symbol = str(config.live.ccf_symbol)
    if ccf_symbol.lower() == "auto":
        ccf_symbol = "CCFG6"
    binance_side = OrderSide.SELL
    if fake_case == "rejected":
        binance_side = OrderSide.BUY
    requests = (
        OrderRequest(
            broker=BrokerName.IBKR_UMC,
            symbol=config.live.binance_symbol,
            side=binance_side,
            quantity=125.5,
            price=720.0,
            timestamp=timestamp,
            row_index=row_index,
            fee_twd=12.3,
            ccf_symbol=ccf_symbol,
            ccf_expiry="2026-02-18",
            contract_policy_state="fake",
        ),
        OrderRequest(
            broker=BrokerName.FUBON_CCF,
            symbol=ccf_symbol,
            side=OrderSide.BUY,
            quantity=3,
            price=1180.0,
            timestamp=timestamp,
            row_index=row_index,
            fee_twd=45.6,
            ccf_symbol=ccf_symbol,
            ccf_expiry="2026-02-18",
            contract_policy_state="fake",
        ),
    )
    return pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_UMC_LONG_CCF,
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
