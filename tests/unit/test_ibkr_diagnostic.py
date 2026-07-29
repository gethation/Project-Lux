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
    ) -> None:
        self.errorEvent = FakeEvent()
        self.contract_count = contract_count
        self.connect_error = connect_error
        self.connected = False
        self.connect_kwargs: dict[str, object] = {}
        self.historical_kwargs: dict[str, object] = {}
        self.market_data_type: int | None = None
        self.cancelled_contract: object | None = None
        self.client = SimpleNamespace(serverVersion=lambda: 178)

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

    def reqMktData(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(marketDataType=3, last=21.39, close=21.29)

    def sleep(self, _seconds: float) -> None:
        return None

    def cancelMktData(self, contract: object) -> None:
        self.cancelled_contract = contract

    def reqHistoricalData(self, *_args: object, **kwargs: object) -> list[object]:
        self.historical_kwargs = kwargs
        return [object()] * 390

    def managedAccounts(self) -> list[str]:
        return ["U1234567"]


def test_diagnostic_uses_readonly_delayed_trades_and_utc_dates() -> None:
    fake = FakeIb()

    result = run_connectivity_diagnostic(
        IbkrDiagnosticConfig(client_id=17_123),
        ib_factory=lambda: fake,
    )

    assert result.server_version == 178
    assert result.accounts == ("U1234567",)
    assert result.con_id == 46_613_372
    assert result.market_data_tier == 3
    assert result.market_data_tier_label == "delayed"
    assert result.historical_bar_count == 390
    assert fake.connect_kwargs["clientId"] == 17_123
    assert fake.connect_kwargs["readonly"] is True
    assert fake.connect_kwargs["fetchFields"].value == 0
    assert fake.market_data_type == 3
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


def test_diagnostic_requires_exactly_one_contract() -> None:
    fake = FakeIb(contract_count=2)

    with pytest.raises(IbkrConnectivityError, match="exactly one"):
        run_connectivity_diagnostic(ib_factory=lambda: fake)

    assert fake.connected is False


def test_diagnostic_surfaces_daily_gateway_login_screen() -> None:
    fake = FakeIb(connect_error=ConnectionRefusedError("refused"))

    with pytest.raises(IbkrConnectivityError, match="daily login screen"):
        run_connectivity_diagnostic(ib_factory=lambda: fake)
