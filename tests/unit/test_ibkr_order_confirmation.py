"""Tiered fill confirmation inside the IBKR worker.

Three tiers, in order of trust: the order's own events, then the order book,
then the account's position delta. The classification they produce is what the
adapter turns into FILLED / FAILED / UNKNOWN, and getting the last of those
wrong is how a partially-filled short becomes an untracked position.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.integrations.ibkr.client_process import (
    IbkrConnectionConfig,
    _IbkrWorkerClient,
)


TS = datetime(2026, 7, 29, 23, 50, tzinfo=TAIPEI_TZ)


class FakeEvent:
    def __iadd__(self, _handler):
        return self


class FakeTrade:
    def __init__(self, *, status, filled, remaining, avg_price=18.9, done_after=0):
        self.order = SimpleNamespace(orderId=77, action="SELL", totalQuantity=406)
        self.contract = SimpleNamespace(symbol="UMC")
        self.orderStatus = SimpleNamespace(
            status=status, filled=filled, remaining=remaining, avgFillPrice=avg_price
        )
        self._done_after = done_after
        self._pumps = 0

    def isDone(self) -> bool:
        return self._pumps >= self._done_after

    def pump(self) -> None:
        self._pumps += 1


class FakeIb:
    def __init__(self, trade, *, positions_before=0.0, positions_after=None):
        self.errorEvent = FakeEvent()
        self.connectedEvent = FakeEvent()
        self.disconnectedEvent = FakeEvent()
        self.trade = trade
        self._positions = [positions_before]
        self._after = positions_before if positions_after is None else positions_after
        self.placed: list = []
        self.sleeps: list[float] = []
        self.open_trades: list = []

    def isConnected(self) -> bool:
        return True

    def reqContractDetails(self, _contract):
        return [SimpleNamespace(contract=SimpleNamespace(symbol="UMC", conId=1))]

    def positions(self):
        quantity = self._positions[0] if not self.placed else self._after
        return [
            SimpleNamespace(
                contract=SimpleNamespace(symbol="UMC"), position=quantity
            )
        ]

    def placeOrder(self, _contract, order):
        self.placed.append(order)
        return self.trade

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.trade.pump()

    def openTrades(self):
        return self.open_trades

    def disconnect(self) -> None:
        return None


def worker(ib: FakeIb) -> _IbkrWorkerClient:
    client = _IbkrWorkerClient(
        IbkrConnectionConfig(client_id=17_999, readonly=False),
        ib_factory=lambda: ib,
        clock=lambda: TS,
    )
    client._ensure_connected = lambda: None  # type: ignore[method-assign]
    return client


def place(ib: FakeIb, **kwargs):
    return worker(ib).place_and_confirm_umc_order(
        action="SELL", quantity=406, wait_seconds=1.0, poll_seconds=0.25, **kwargs
    )


def test_tier_one_a_filled_event_resolves_immediately() -> None:
    ib = FakeIb(
        FakeTrade(status="Filled", filled=406.0, remaining=0.0, done_after=0),
        positions_before=0.0,
        positions_after=-406.0,
    )

    result = place(ib)

    assert result["classification"] == "filled"
    assert result["filled"] == 406.0
    # Resolved before any pumping was needed.
    assert ib.sleeps == []


def test_tier_two_the_order_book_answers_when_events_do_not() -> None:
    """A missed event must not become an unknown while the book knows."""
    trade = FakeTrade(status="Submitted", filled=0.0, remaining=406.0, done_after=99)
    ib = FakeIb(trade, positions_before=0.0, positions_after=-406.0)
    ib.open_trades = [
        SimpleNamespace(
            order=SimpleNamespace(orderId=77, action="SELL", totalQuantity=406),
            contract=SimpleNamespace(symbol="UMC"),
            orderStatus=SimpleNamespace(status="Filled", filled=406.0, remaining=0.0),
        )
    ]

    result = place(ib)

    assert result["classification"] == "filled"
    assert result["status"] == "Filled"


def test_tier_three_the_position_delta_is_the_last_resort() -> None:
    """IBKR said nothing useful, but the account moved by exactly this order."""
    trade = FakeTrade(status="Submitted", filled=0.0, remaining=406.0, done_after=99)
    ib = FakeIb(trade, positions_before=0.0, positions_after=-406.0)

    result = place(ib)

    assert result["classification"] == "filled"
    assert result["delta_confirms"] is True
    assert result["observed_delta"] == -406.0


def test_a_delta_that_only_half_matches_is_not_a_confirmation() -> None:
    """A partial move says the position changed for some other reason too."""
    trade = FakeTrade(status="Submitted", filled=0.0, remaining=406.0, done_after=99)
    ib = FakeIb(trade, positions_before=0.0, positions_after=-200.0)

    result = place(ib)

    assert result["classification"] == "unknown"
    assert result["delta_confirms"] is False


def test_terminal_and_unmoved_is_failed_not_unknown() -> None:
    """Cancelled, nothing filled, position unchanged: safe, no exposure."""
    trade = FakeTrade(status="Cancelled", filled=0.0, remaining=406.0, done_after=0)
    ib = FakeIb(trade, positions_before=0.0, positions_after=0.0)

    result = place(ib)

    assert result["classification"] == "failed"


def test_a_timeout_with_nothing_terminal_is_unknown() -> None:
    """Still working when the deadline passed. It may yet fill."""
    trade = FakeTrade(status="Submitted", filled=0.0, remaining=406.0, done_after=99)
    ib = FakeIb(trade, positions_before=0.0, positions_after=0.0)

    result = place(ib)

    assert result["classification"] == "unknown"
    assert ib.sleeps  # it waited before giving up


def test_a_partial_fill_is_unknown_rather_than_filled() -> None:
    trade = FakeTrade(status="Submitted", filled=120.0, remaining=286.0, done_after=99)
    ib = FakeIb(trade, positions_before=0.0, positions_after=-120.0)

    result = place(ib)

    assert result["classification"] == "unknown"


def test_fractional_share_quantities_are_refused_at_the_worker_too() -> None:
    """Belt and braces: the adapter rounds, but the worker will not take a
    fractional share count even if something else calls it."""
    ib = FakeIb(FakeTrade(status="Filled", filled=406.0, remaining=0.0))

    with pytest.raises(ValueError, match="cash equity"):
        worker(ib).place_and_confirm_umc_order(
            action="SELL", quantity=406.5, wait_seconds=1.0
        )
    assert ib.placed == []


def test_position_query_sums_umc_rows_and_ignores_others() -> None:
    ib = FakeIb(FakeTrade(status="Filled", filled=0.0, remaining=0.0))
    ib.positions = lambda: [  # type: ignore[method-assign]
        SimpleNamespace(contract=SimpleNamespace(symbol="UMC"), position=-200.0),
        SimpleNamespace(contract=SimpleNamespace(symbol="UMC"), position=-206.0),
        SimpleNamespace(contract=SimpleNamespace(symbol="TSM"), position=999.0),
    ]

    assert worker(ib).fetch_umc_position() == -406.0
