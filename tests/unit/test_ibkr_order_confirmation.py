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


class FakeFill:
    def __init__(self, order_id: int, shares: float, symbol: str = "UMC"):
        self.execution = SimpleNamespace(orderId=order_id, shares=shares)
        self.contract = SimpleNamespace(symbol=symbol)


class FakeIb:
    def __init__(
        self,
        trade,
        *,
        positions_before=0.0,
        positions_after=None,
        fills_after_pumps: int | None = None,
        fill_shares: float = 0.0,
        fill_order_id: int = 77,
    ):
        # `fills_after_pumps` models what a real Gateway did on 2026-08-04: the
        # order status went terminal, and the execution appeared a second later.
        self.fills_after_pumps = fills_after_pumps
        self.fill_shares = fill_shares
        self.fill_order_id = fill_order_id
        self.execution_requests = 0
        self._pumps = 0
        self._init_rest(trade, positions_before, positions_after)

    def fills(self):
        if self.fills_after_pumps is None or self._pumps < self.fills_after_pumps:
            return []
        return [FakeFill(self.fill_order_id, self.fill_shares)]

    def reqExecutions(self, _filter=None):
        self.execution_requests += 1
        return []

    def _init_rest(self, trade, positions_before, positions_after):
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
        self._pumps += 1
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


def test_a_cancelled_status_that_later_executes_is_filled_not_failed() -> None:
    """REGRESSION, observed on a real Gateway 2026-08-04.

    IBKR emits warning 10349 ("order TIF set to DAY per preset"); ib_async
    treats it as fatal and marks the Trade Cancelled with filled=0. IBKR then
    executes the order about a second later.

    The old code read one position snapshot the instant the terminal status
    arrived -- before the fill landed -- saw no movement, and returned `failed`,
    which the caller acts on as "nothing was left open". Both live smoke orders
    did this. The buy reported safe while holding a share; the sell reported
    CRITICAL while already flat.

    `failed` now has to be earned by waiting for the account to catch up.
    """
    trade = FakeTrade(status="Cancelled", filled=0.0, remaining=406.0, done_after=0)
    ib = FakeIb(
        trade,
        positions_before=0.0,
        # The position view lags too, so it cannot rescue this on its own.
        positions_after=0.0,
        fills_after_pumps=2,
        fill_shares=406.0,
    )

    result = place(ib)

    assert result["classification"] == "filled"
    assert result["execution_shares"] == 406.0
    assert result["executions_confirm"] is True
    assert ib.sleeps, "it must wait for the account before concluding"


def test_a_late_partial_execution_is_unknown_not_failed() -> None:
    """The same lag, but only part of the order traded. Not safe, not clean."""
    trade = FakeTrade(status="Cancelled", filled=0.0, remaining=406.0, done_after=0)
    ib = FakeIb(
        trade,
        positions_before=0.0,
        positions_after=0.0,
        fills_after_pumps=2,
        fill_shares=120.0,
    )

    result = place(ib)

    assert result["classification"] == "unknown"
    assert result["execution_shares"] == 120.0


def test_a_genuine_rejection_still_settles_to_failed() -> None:
    """The waiting must not turn every rejection into an unknown -- a real
    rejection is safe, and pausing on it would stop the pair for nothing."""
    trade = FakeTrade(status="Cancelled", filled=0.0, remaining=406.0, done_after=0)
    ib = FakeIb(trade, positions_before=0.0, positions_after=0.0)

    result = place(ib, settle_seconds=1.0)

    assert result["classification"] == "failed"
    assert result["execution_shares"] == 0.0
    # It asked the account rather than trusting the status alone.
    assert ib.execution_requests > 0


def test_executions_for_another_order_do_not_confirm_this_one() -> None:
    """A concurrent fill on a different order id is not this order's evidence."""
    trade = FakeTrade(status="Cancelled", filled=0.0, remaining=406.0, done_after=0)
    ib = FakeIb(
        trade,
        positions_before=0.0,
        positions_after=0.0,
        fills_after_pumps=1,
        fill_shares=406.0,
        fill_order_id=999,
    )

    result = place(ib, settle_seconds=1.0)

    assert result["classification"] == "failed"
    assert result["execution_shares"] == 0.0


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
