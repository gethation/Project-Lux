"""Mid-hold detection that the UMC leg moved without us.

UMC is the only leg a third party can close on its own -- a stock-loan recall
buys in a short whenever the lender asks. Before this, the gap between that
happening and anyone noticing was the rest of the hold.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import BrokerName
from lux_trader.reconciliation.models import (
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
)
from lux_trader.reconciliation.position_drift import (
    compare_umc_position,
    report_position_drift,
    umc_position_from,
)


TS = datetime.fromisoformat("2026-07-29T23:15:00+08:00")


def snapshot(*positions: tuple[str, float]) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker=BrokerName.IBKR_UMC,
        account_id="U1234567",
        fetched_at=TS,
        positions=tuple(
            BrokerPositionSnapshot(
                broker=BrokerName.IBKR_UMC, symbol=symbol, quantity=quantity
            )
            for symbol, quantity in positions
        ),
    )


def test_a_matching_short_is_not_drift() -> None:
    drift = compare_umc_position(
        snapshot(("UMC", -1200.0)), symbol="UMC", expected_units=-1200.0
    )

    assert not drift.drifted
    assert drift.detail == "matches the strategy position"


def test_a_fully_recalled_short_is_drift_and_says_so() -> None:
    """IBKR reports nothing; the CCF leg is now uncovered."""
    drift = compare_umc_position(
        snapshot(), symbol="UMC", expected_units=-1200.0
    )

    assert drift.drifted
    assert drift.observed == 0.0
    assert "closed without us" in drift.detail


def test_a_partial_buy_in_is_drift() -> None:
    drift = compare_umc_position(
        snapshot(("UMC", -600.0)), symbol="UMC", expected_units=-1200.0
    )

    assert drift.drifted
    assert "partially closed without us" in drift.detail


def test_a_position_appearing_from_nowhere_is_drift() -> None:
    """Flat in our books, held at the broker -- the other direction, equally wrong."""
    drift = compare_umc_position(
        snapshot(("UMC", 400.0)), symbol="UMC", expected_units=0.0
    )

    assert drift.drifted


def test_split_rows_for_one_symbol_are_summed() -> None:
    drift = compare_umc_position(
        snapshot(("UMC", -700.0), ("UMC", -500.0)),
        symbol="UMC",
        expected_units=-1200.0,
    )

    assert not drift.drifted


def test_other_symbols_are_ignored() -> None:
    drift = compare_umc_position(
        snapshot(("TSM", -9999.0), ("UMC", -1200.0)),
        symbol="UMC",
        expected_units=-1200.0,
    )

    assert not drift.drifted
    assert umc_position_from(snapshot(("TSM", -9999.0)), "UMC") == 0.0


def test_tolerance_admits_float_dust() -> None:
    drift = compare_umc_position(
        snapshot(("UMC", -1200.0000001)),
        symbol="UMC",
        expected_units=-1200.0,
        tolerance=1e-6,
    )

    assert not drift.drifted


def test_drift_is_reported_as_an_error_not_a_warning() -> None:
    """An uncovered leg in a market-neutral strategy is not warning-level, and
    error is what reaches the ntfy errors topic."""

    class RecordingStore:
        def __init__(self) -> None:
            self.events: list[tuple] = []

        def record_event(self, row_index, timestamp, event_type, message, payload):
            self.events.append((event_type, message, payload))

    class RecordingReporter:
        def __init__(self) -> None:
            self.errors: list[str] = []
            self.warnings: list[str] = []

        def error(self, _timestamp, message):
            self.errors.append(message)

        def warn(self, _timestamp, code, detail=""):
            self.warnings.append(code)

    store, reporter = RecordingStore(), RecordingReporter()
    drift = compare_umc_position(snapshot(), symbol="UMC", expected_units=-1200.0)

    report_position_drift(drift, checked_at=TS, store=store, reporter=reporter)

    assert reporter.warnings == []
    assert len(reporter.errors) == 1
    assert "umc_position_drift" in reporter.errors[0]
    assert store.events[0][0] == "umc_position_drift"
    assert store.events[0][2]["observed"] == 0.0
    assert store.events[0][2]["expected"] == -1200.0
