"""The probe that decides whether the UMC leg can trade today.

Before 2026-08-03 this asked for delayed data and reported no book, so it was
structurally incapable of answering either question it exists for. These tests
pin both fixes: it asks for live, and it reports bid/ask.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from lux_trader.integrations.ibkr.diagnostic import (
    IbkrConnectivityError,
    IbkrDiagnosticConfig,
    run_connectivity_diagnostic,
)


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object) -> "FakeEvent":
        self.handlers.append(handler)
        return self

    def __isub__(self, handler: object) -> "FakeEvent":
        self.handlers.remove(handler)
        return self

    def emit(self, *args: object) -> None:
        for handler in list(self.handlers):
            handler(*args)


@dataclass
class FakeContract:
    conId: int = 46_613_372
    symbol: str = "UMC"
    exchange: str = "SMART"
    primaryExchange: str = "NYSE"
    currency: str = "USD"


class FakeIb:
    def __init__(
        self,
        *,
        contract_count: int = 1,
        connect_error: Exception | None = None,
        ticker: object | None = None,
        quote_error: tuple[int, str] | None = None,
    ) -> None:
        self.errorEvent = FakeEvent()
        self.contract_count = contract_count
        self.connect_error = connect_error
        self.quote_error = quote_error
        self.connected = False
        self.connect_kwargs: dict[str, object] = {}
        self.historical_kwargs: dict[str, object] = {}
        self.market_data_type: int | None = None
        self.generic_tick_list: str | None = None
        self.cancelled_contract: object | None = None
        self.client = SimpleNamespace(serverVersion=lambda: 178)
        self.ticker = ticker or SimpleNamespace(
            marketDataType=1,
            last=18.415,
            close=19.03,
            bid=18.41,
            ask=18.42,
            bidSize=5500.0,
            askSize=1900.0,
            shortableShares=1_950_065.0,
            shortable=3.0,
        )

    def connect(self, host: str, port: int, **kwargs: object) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.connect_kwargs = {"host": host, "port": port, **kwargs}

    def isConnected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False

    def reqContractDetails(self, _requested: object) -> list[object]:
        detail = SimpleNamespace(
            contract=FakeContract(),
            longName="UNITED MICROELECTRON-SP ADR",
            timeZoneId="US/Eastern",
            tradingHours="20260723:0400-20260723:2000",
        )
        return [detail] * self.contract_count

    def reqMarketDataType(self, tier: int) -> None:
        self.market_data_type = tier

    def reqMktData(self, *_args: object, **kwargs: object) -> object:
        self.generic_tick_list = kwargs.get("genericTickList")
        if self.quote_error is not None:
            code, message = self.quote_error
            self.errorEvent.emit(1, code, message, None)
        return self.ticker

    def sleep(self, _seconds: float) -> None:
        return None

    def cancelMktData(self, contract: object) -> None:
        self.cancelled_contract = contract

    def reqHistoricalData(self, *_args: object, **kwargs: object) -> list[object]:
        self.historical_kwargs = kwargs
        return [object()] * 390

    def managedAccounts(self) -> list[str]:
        return ["U1234567"]


def test_the_probe_asks_for_live_not_delayed() -> None:
    """THE BUG THIS REPLACES. Requesting 3 overrides an entitlement the account
    holds, so a probe pinned to delayed can only ever report delayed -- the one
    answer that tells you nothing about whether the subscription arrived."""
    fake = FakeIb()

    result = run_connectivity_diagnostic(
        IbkrDiagnosticConfig(client_id=17_123),
        ib_factory=lambda: fake,
    )

    assert fake.market_data_type == 1
    assert result.market_data_type_requested == 1
    assert result.market_data_tier == 1
    assert result.market_data_tier_label == "live"
    assert result.live_tier_granted is True


def test_the_probe_reports_the_book() -> None:
    """The other half of the bug: this branch scores entries on a directional
    bid/ask z-score, so a probe that reports only `last` calls a bookless feed
    healthy."""
    result = run_connectivity_diagnostic(ib_factory=lambda: FakeIb())

    assert result.quote_bid == pytest.approx(18.41)
    assert result.quote_ask == pytest.approx(18.42)
    assert result.quote_bid_size == pytest.approx(5500.0)
    assert result.quote_ask_size == pytest.approx(1900.0)
    assert result.book_available is True


def test_a_last_price_with_no_book_is_not_a_pass() -> None:
    """Exactly what delayed data looks like: a tradeable-looking price and
    nothing to trade against. The system would run all session and take no
    entries."""
    bookless = SimpleNamespace(
        marketDataType=1, last=18.415, close=19.03,
        bid=float("nan"), ask=float("nan"), bidSize=None, askSize=None,
        shortableShares=float("nan"), shortable=float("nan"),
    )

    result = run_connectivity_diagnostic(
        ib_factory=lambda: FakeIb(ticker=bookless)
    )

    assert result.quote_last == pytest.approx(18.415)
    assert result.book_available is False


def test_borrow_availability_is_reported() -> None:
    """A margin account is not a borrowable symbol, and about half the
    backtest's trades sell UMC first."""
    fake = FakeIb()

    result = run_connectivity_diagnostic(ib_factory=lambda: fake)

    assert fake.generic_tick_list == "236"
    assert result.shortable_shares == pytest.approx(1_950_065.0)
    assert result.shortable_rank == pytest.approx(3.0)
    assert result.shortable is True


def test_an_absent_shortable_tick_is_unknown_not_no() -> None:
    """IBKR does not publish 236 for every symbol. Reporting that as 'cannot
    borrow' would block a leg that is actually fine."""
    silent = SimpleNamespace(
        marketDataType=1, last=18.4, close=19.0, bid=18.41, ask=18.42,
        bidSize=1.0, askSize=1.0,
        shortableShares=float("nan"), shortable=float("nan"),
    )

    result = run_connectivity_diagnostic(ib_factory=lambda: FakeIb(ticker=silent))

    assert result.shortable_rank is None
    assert result.shortable is None
    assert result.book_available is True


def test_a_hard_to_borrow_rank_is_reported_as_not_borrowable() -> None:
    hard = SimpleNamespace(
        marketDataType=1, last=18.4, close=19.0, bid=18.41, ask=18.42,
        bidSize=1.0, askSize=1.0, shortableShares=100.0, shortable=1.0,
    )

    result = run_connectivity_diagnostic(ib_factory=lambda: FakeIb(ticker=hard))

    assert result.shortable is False


def test_an_entitlement_error_is_separated_from_a_quiet_market() -> None:
    """10089 and 'no book, no error' need different fixes -- buy a subscription
    versus check the feed -- so the probe must not collapse them."""
    bookless = SimpleNamespace(
        marketDataType=1, last=float("nan"), close=float("nan"),
        bid=float("nan"), ask=float("nan"), bidSize=None, askSize=None,
        shortableShares=float("nan"), shortable=float("nan"),
    )
    fake = FakeIb(
        ticker=bookless,
        quote_error=(10089, "Requested market data requires additional subscription"),
    )

    result = run_connectivity_diagnostic(ib_factory=lambda: fake)

    assert result.entitlement_error_code == 10089
    assert "additional subscription" in result.entitlement_error_message
    assert result.book_available is False


def test_a_healthy_probe_reports_no_entitlement_error() -> None:
    result = run_connectivity_diagnostic(ib_factory=lambda: FakeIb())

    assert result.entitlement_error_code is None
    assert result.entitlement_error_message is None


def test_the_delayed_tier_can_still_be_requested_explicitly() -> None:
    """Overridable, so a future no-entitlement window can be diagnosed -- but
    it has to be asked for, never defaulted to."""
    fake = FakeIb()

    run_connectivity_diagnostic(
        IbkrDiagnosticConfig(market_data_type=3),
        ib_factory=lambda: fake,
    )

    assert fake.market_data_type == 3


def test_connection_stays_readonly_and_historical_stays_pinned() -> None:
    fake = FakeIb()

    result = run_connectivity_diagnostic(
        IbkrDiagnosticConfig(client_id=17_123),
        ib_factory=lambda: fake,
    )

    assert result.server_version == 178
    assert result.accounts == ("U1234567",)
    assert result.con_id == 46_613_372
    assert result.historical_bar_count == 390
    assert fake.connect_kwargs["clientId"] == 17_123
    assert fake.connect_kwargs["readonly"] is True
    assert fake.connect_kwargs["fetchFields"].value == 0
    assert fake.historical_kwargs == {
        "endDateTime": "",
        "durationStr": "1 D",
        "barSizeSetting": "1 min",
        "whatToShow": "TRADES",
        "useRTH": True,
        "formatDate": 2,
        "keepUpToDate": False,
        "timeout": 60.0,
    }
    assert fake.cancelled_contract is not None
    assert fake.connected is False


def test_the_probed_symbol_is_configurable() -> None:
    result = run_connectivity_diagnostic(
        IbkrDiagnosticConfig(symbol="UMC"),
        ib_factory=lambda: FakeIb(),
    )

    assert result.symbol == "UMC"

    with pytest.raises(ValueError, match="symbol must not be empty"):
        run_connectivity_diagnostic(
            IbkrDiagnosticConfig(symbol="  "), ib_factory=lambda: FakeIb()
        )


def test_diagnostic_requires_exactly_one_contract() -> None:
    fake = FakeIb(contract_count=2)

    with pytest.raises(IbkrConnectivityError, match="exactly one"):
        run_connectivity_diagnostic(ib_factory=lambda: fake)

    assert fake.connected is False


def test_diagnostic_surfaces_daily_gateway_login_screen() -> None:
    fake = FakeIb(connect_error=ConnectionRefusedError("refused"))

    with pytest.raises(IbkrConnectivityError, match="daily login screen"):
        run_connectivity_diagnostic(ib_factory=lambda: fake)


def test_data_farm_status_notices_are_not_reported_as_errors() -> None:
    """IBKR sends "HMDS data farm connection is OK" (2106) down the error
    channel on every healthy connection. Reporting it as a warning is how a
    reader learns to skim the line that will one day matter."""

    class ChattyIb(FakeIb):
        def reqHistoricalData(self, *args: object, **kwargs: object) -> list[object]:
            self.errorEvent.emit(2, 2106, "HMDS data farm connection is OK:ushmds", None)
            return super().reqHistoricalData(*args, **kwargs)

    result = run_connectivity_diagnostic(ib_factory=lambda: ChattyIb())

    assert result.historical_error_code is None
    assert result.historical_bar_count == 390


def test_a_real_historical_failure_still_surfaces() -> None:
    class BrokenIb(FakeIb):
        def reqHistoricalData(self, *_args: object, **_kwargs: object) -> list[object]:
            self.errorEvent.emit(2, 162, "Historical Market Data Service error", None)
            return []

    result = run_connectivity_diagnostic(ib_factory=lambda: BrokenIb())

    assert result.historical_error_code == 162
    assert result.historical_bar_count == 0


def test_to_dict_carries_the_verdicts_not_just_the_fields() -> None:
    """The JSON form is what gets pasted into an incident note; the three
    computed verdicts are the part a reader actually needs."""
    payload = run_connectivity_diagnostic(ib_factory=lambda: FakeIb()).to_dict()

    assert payload["book_available"] is True
    assert payload["live_tier_granted"] is True
    assert payload["shortable"] is True
    assert payload["accounts"] == ["U1234567"]
