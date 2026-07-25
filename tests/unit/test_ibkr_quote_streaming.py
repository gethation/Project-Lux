"""The IBKR quote subscription must not serve a frozen snapshot.

Found live on 2026-07-25: 485 consecutive fetches returned a byte-identical
payload -- same price, same exchange timestamp, and ticker.time frozen at the
instant of the FIRST fetch. The old code subscribed and cancelled per quote,
but ib_async hands back the same cached Ticker for a contract and
cancelMktData does not clear its fields, so a wait loop that stopped at "any
finite value present" returned the previous call's values instantly, forever.

The existing FakeIb hid this because it built a fresh SimpleNamespace on every
reqMktData call. The fake here behaves like the real library: one Ticker per
contract, mutated in place as ticks arrive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.integrations.ibkr.client_process import (
    IbkrConnectionConfig,
    _IbkrWorkerClient,
)


START = datetime(2026, 7, 25, 0, 41, 38, tzinfo=TAIPEI_TZ)


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object) -> "FakeEvent":
        self.handlers.append(handler)
        return self

    def emit(self, *args: object) -> None:
        for handler in list(self.handlers):
            handler(*args)


class StreamingFakeIb:
    """ib_async-shaped fake: one cached Ticker per contract, mutated in place.

    ``ticks_per_pump`` controls how the stream behaves while the event loop is
    pumped -- zero models a wedged or simply quiet stream.
    """

    def __init__(self, *, ticks_per_pump: int = 1) -> None:
        self.errorEvent = FakeEvent()
        self.connectedEvent = FakeEvent()
        self.disconnectedEvent = FakeEvent()
        self.connected = False
        self.client = SimpleNamespace(serverVersion=lambda: 178)
        self.ticks_per_pump = ticks_per_pump
        self.req_mkt_data_calls = 0
        self.cancel_mkt_data_calls = 0
        self.market_data_type_calls: list[int] = []
        self._tick_count = 0
        self._ticker = SimpleNamespace(
            marketDataType=3,
            last=19.425,
            close=20.87,
            bid=None,
            ask=None,
            bidSize=None,
            askSize=None,
            time=START,
            lastTimestamp=None,
            delayedLastTimestamp=int(START.timestamp()),
        )

    # -- connection -----------------------------------------------------
    def connect(self, *_args: object, **_kwargs: object) -> None:
        self.connected = True
        self.connectedEvent.emit()

    def isConnected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False
        self.disconnectedEvent.emit()

    def managedAccounts(self) -> list[str]:
        return ["U19069698"]

    # -- contract -------------------------------------------------------
    def reqContractDetails(self, _contract: object) -> list[object]:
        contract = SimpleNamespace(
            conId=46_613_372,
            symbol="UMC",
            exchange="SMART",
            primaryExchange="NYSE",
            currency="USD",
        )
        return [
            SimpleNamespace(
                contract=contract,
                longName="UNITED MICROELECTRON-SP ADR",
                timeZoneId="US/Eastern",
                tradingHours="20260725:0400-20260725:2000",
                liquidHours="20260725:0930-20260725:1600",
            )
        ]

    # -- market data ----------------------------------------------------
    def reqMarketDataType(self, tier: int) -> None:
        self.market_data_type_calls.append(int(tier))

    def reqMktData(self, *_args: object, **_kwargs: object) -> object:
        self.req_mkt_data_calls += 1
        return self._ticker

    def cancelMktData(self, _contract: object) -> None:
        self.cancel_mkt_data_calls += 1

    def sleep(self, _seconds: float) -> None:
        """Pumping the loop is what delivers ticks in ib_async."""
        for _ in range(self.ticks_per_pump):
            self._tick_count += 1
            self._ticker.time = START + timedelta(seconds=self._tick_count)
            self._ticker.last = 19.425 + self._tick_count * 0.005
            self._ticker.delayedLastTimestamp = int(
                (START + timedelta(seconds=self._tick_count)).timestamp()
            )


def worker(ib: StreamingFakeIb) -> _IbkrWorkerClient:
    return _IbkrWorkerClient(
        IbkrConnectionConfig(client_id=17_555),
        ib_factory=lambda: ib,
        clock=lambda: datetime(2026, 7, 25, 1, 0, tzinfo=TAIPEI_TZ),
    )


def test_repeated_quotes_advance_instead_of_freezing() -> None:
    """The bug, stated as a test: consecutive fetches must not be identical."""
    ib = StreamingFakeIb()
    client = worker(ib)

    quotes = [
        client.fetch_umc_quote(quote_wait_timeout_seconds=5.0) for _ in range(5)
    ]

    assert len({q["ticker_time"] for q in quotes}) == 5
    assert len({q["last"] for q in quotes}) == 5
    assert all(q["ticker_advanced"] for q in quotes)


def test_the_subscription_is_opened_once_and_kept() -> None:
    """485 subscribe/cancel cycles wedged the real Gateway; one is enough."""
    ib = StreamingFakeIb()
    client = worker(ib)

    for _ in range(20):
        client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)

    assert ib.req_mkt_data_calls == 1
    assert ib.cancel_mkt_data_calls == 0
    assert ib.market_data_type_calls == [3]


def test_a_wedged_stream_is_reported_rather_than_hidden() -> None:
    """A stream that stops ticking still returns -- with advanced=False and an
    unchanged timestamp, so the staleness budget downstream can reject it."""
    ib = StreamingFakeIb(ticks_per_pump=1)
    client = worker(ib)

    first = client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)
    ib.ticks_per_pump = 0  # the stream wedges
    second = client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)
    third = client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)

    assert first["ticker_advanced"] is True
    assert second["ticker_advanced"] is False
    assert third["ticker_advanced"] is False
    # The timestamp stays honest rather than being restamped as fresh.
    assert second["ticker_time"] == first["ticker_time"]
    assert second["delayed_last_timestamp"] == first["delayed_last_timestamp"]


def test_a_quiet_stream_does_not_stall_the_loop() -> None:
    """Blocking until the ticker advances would freeze the live loop whenever
    the market is quiet. Steady-state fetches pump once and return."""
    ib = StreamingFakeIb(ticks_per_pump=1)
    client = worker(ib)
    client.fetch_umc_quote(quote_wait_timeout_seconds=30.0)

    ib.ticks_per_pump = 0
    sleep_calls = []
    original_sleep = ib.sleep

    def counting_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        original_sleep(seconds)

    ib.sleep = counting_sleep
    client.fetch_umc_quote(quote_wait_timeout_seconds=30.0)

    # One short pump, not thirty seconds of waiting.
    assert len(sleep_calls) == 1
    assert sum(sleep_calls) < 1.0


def test_the_first_fetch_waits_for_the_stream_to_deliver() -> None:
    """A brand-new subscription has no values yet, so bootstrapping still waits."""
    ib = StreamingFakeIb(ticks_per_pump=1)
    ib._ticker.marketDataType = None
    ib._ticker.last = None
    ib._ticker.close = None

    pumps = {"n": 0}
    original_sleep = ib.sleep

    def waking_sleep(seconds: float) -> None:
        pumps["n"] += 1
        if pumps["n"] >= 3:
            ib._ticker.marketDataType = 3
            ib._ticker.last = 19.5
        original_sleep(seconds)

    ib.sleep = waking_sleep
    client = worker(ib)

    quote = client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)

    assert pumps["n"] >= 3
    assert quote["last"] == pytest.approx(19.5 + 0.0, abs=0.1)
    assert quote["market_data_tier"] == 3


def test_disconnect_drops_the_ticker_so_a_reconnect_resubscribes() -> None:
    """Holding a dead session's ticker would serve its last values forever."""
    ib = StreamingFakeIb()
    client = worker(ib)
    client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)
    assert ib.req_mkt_data_calls == 1

    ib.disconnect()
    client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)

    assert ib.req_mkt_data_calls == 2


def test_close_releases_the_market_data_line() -> None:
    ib = StreamingFakeIb()
    client = worker(ib)
    client.fetch_umc_quote(quote_wait_timeout_seconds=5.0)

    client.close()

    assert ib.cancel_mkt_data_calls == 1
    assert ib.connected is False
