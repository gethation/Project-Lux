"""A shared broker account holds more than one pair's positions.

Making the Fubon execution worker symbol-agnostic removed the symbol it used to
filter account snapshots by, and that filtering could not simply move back: the
margin view is deliberately account-wide, so the snapshot must be too. Filtering
therefore belongs to the reconciler, which already knows this pair's symbols.

Without ``sibling_symbols`` the CCF position shows up as a ghost while
reconciling QFF, and a perfectly healthy pair is paused for it -- exactly the
cross-pair interference the multi-pair design exists to prevent.
"""

from __future__ import annotations

from datetime import datetime

from lux_trader.core.models import BrokerName, Direction, StrategyState
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.reconciliation import ReconciliationStatus
from lux_trader.reconciliation.brokers import (
    BrokerPositionSnapshot,
    FakeReadOnlyBroker,
)
from lux_trader.reconciliation.reconciler import BrokerReconciler


QFF = "QFFG6"
CCF = "CCFH6"
TSM = "TSM/USDT:USDT"
NOW = datetime.fromisoformat("2026-07-26T10:00:00+08:00")


def position(
    symbol: str,
    quantity: float,
    broker: BrokerName = BrokerName.FUBON,
) -> BrokerPositionSnapshot:
    return BrokerPositionSnapshot(
        broker=broker,
        symbol=symbol,
        quantity=quantity,
        raw={},
    )


def qff_state() -> StrategyRuntimeState:
    state = StrategyRuntimeState(state=StrategyState.OPEN)
    state.position_direction = Direction.SHORT_US_LONG_TW
    state.tw_leg_contracts = 9
    state.us_leg_units = -1000.0
    state.trading_tw_leg_symbol = QFF
    return state


def account_with_both_pairs() -> tuple:
    """One Fubon account holding both pairs' futures, plus the Binance leg."""
    return (
        FakeReadOnlyBroker(
            BrokerName.FUBON,
            positions=(position(QFF, 9.0), position(CCF, 1.0)),
            fetched_at=NOW,
        ),
        FakeReadOnlyBroker(
            BrokerName.BINANCE,
            positions=(position(TSM, -1000.0, BrokerName.BINANCE),),
            fetched_at=NOW,
        ),
    )


def test_a_sibling_pairs_position_is_not_a_ghost() -> None:
    report = BrokerReconciler().reconcile(
        strategy_state=qff_state(),
        brokers=account_with_both_pairs(),
        us_leg_symbol=TSM,
        tw_leg_symbol=QFF,
        timestamp=NOW,
        sibling_symbols=frozenset({CCF}),
    )

    assert report.status == ReconciliationStatus.MATCHED
    assert report.issues == ()


def test_without_declaring_the_sibling_it_is_flagged() -> None:
    """The check still exists -- this is the same input, undeclared."""
    report = BrokerReconciler().reconcile(
        strategy_state=qff_state(),
        brokers=account_with_both_pairs(),
        us_leg_symbol=TSM,
        tw_leg_symbol=QFF,
        timestamp=NOW,
    )

    assert report.status != ReconciliationStatus.MATCHED
    flagged = [issue for issue in report.issues if issue.symbol == CCF]
    assert len(flagged) == 1
    assert flagged[0].issue_type == "unexpected_position"


def test_a_genuinely_unknown_contract_is_still_flagged() -> None:
    """Declaring one sibling must not blanket-silence every other symbol."""
    brokers = (
        FakeReadOnlyBroker(
            BrokerName.FUBON,
            positions=(
                position(QFF, 9.0),
                position(CCF, 1.0),
                position("TXFG6", 3.0),  # nobody's pair
            ),
            fetched_at=NOW,
        ),
        FakeReadOnlyBroker(
            BrokerName.BINANCE,
            positions=(position(TSM, -1000.0, BrokerName.BINANCE),),
            fetched_at=NOW,
        ),
    )

    report = BrokerReconciler().reconcile(
        strategy_state=qff_state(),
        brokers=brokers,
        us_leg_symbol=TSM,
        tw_leg_symbol=QFF,
        timestamp=NOW,
        sibling_symbols=frozenset({CCF}),
    )

    flagged = [issue for issue in report.issues if issue.issue_type == "unexpected_position"]
    assert [issue.symbol for issue in flagged] == ["TXFG6"]


def test_this_pairs_own_mismatch_is_still_caught() -> None:
    """Silencing siblings must not silence the pair being reconciled."""
    brokers = (
        FakeReadOnlyBroker(
            BrokerName.FUBON,
            positions=(position(QFF, 4.0), position(CCF, 1.0)),
            fetched_at=NOW,
        ),
        FakeReadOnlyBroker(
            BrokerName.BINANCE,
            positions=(position(TSM, -1000.0, BrokerName.BINANCE),),
            fetched_at=NOW,
        ),
    )

    report = BrokerReconciler().reconcile(
        strategy_state=qff_state(),
        brokers=brokers,
        us_leg_symbol=TSM,
        tw_leg_symbol=QFF,
        timestamp=NOW,
        sibling_symbols=frozenset({CCF}),
    )

    assert report.status != ReconciliationStatus.MATCHED
    mismatches = [
        issue
        for issue in report.issues
        if issue.issue_type == "position_quantity_mismatch"
    ]
    assert [issue.symbol for issue in mismatches] == [QFF]
