from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from lux_trader.config import MarginManagementConfig
from lux_trader.core.models import BrokerName
from lux_trader.margin.display import AccountDisplay, AccountDisplayProvider
from lux_trader.reconciliation import FakeReadOnlyBroker
from lux_trader.reconciliation.models import BrokerAccountSnapshot, BrokerMarginSnapshot


NOTIONAL = 1_000_000.0
RATE = 30.0  # USD/TWD


def config() -> SimpleNamespace:
    # AccountDisplayProvider only reads config.margin_management ratios.
    return SimpleNamespace(margin_management=MarginManagementConfig())


def umc_margin(*, upnl: float | None = 500.0, equity: float = 10_000.0) -> BrokerMarginSnapshot:
    # IBKR account-summary tag names, flattened by IbkrReadOnlyBroker. There is
    # no equity-minus-wallet derivation here: IBKR reports UnrealizedPnL
    # directly, so an absent tag means the panel shows blank.
    raw: dict[str, object] = {"MaintMarginReq": 0.0}
    if upnl is not None:
        raw["UnrealizedPnL"] = upnl
    return BrokerMarginSnapshot(
        broker=BrokerName.IBKR_UMC,
        currency="USD",
        equity=equity,
        available=equity,
        margin_used=0.0,
        raw=raw,
    )


def fubon_margin(*, upnl: float | None = -20_000.0, equity: float = 900_000.0) -> BrokerMarginSnapshot:
    raw: dict[str, object] = {}
    if upnl is not None:
        raw["unrealized_pnl"] = upnl
    return BrokerMarginSnapshot(
        broker=BrokerName.FUBON_CCF,
        currency="TWD",
        equity=equity,
        available=equity,
        margin_used=0.0,
        raw=raw,
    )


def provider(brokers, *, rate: float | None = RATE) -> AccountDisplayProvider:
    return AccountDisplayProvider(
        config(),
        usd_twd_rate=lambda: rate,
        brokers_factory=lambda: brokers,
    )


def fakes(umc: tuple = (), fubon: tuple = ()) -> tuple:
    # Provider expects (fubon, umc).
    return (
        FakeReadOnlyBroker(BrokerName.FUBON_CCF, margins=fubon),
        FakeReadOnlyBroker(BrokerName.IBKR_UMC, margins=umc),
    )


def test_combined_upnl_and_ratios_from_snapshots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    p = provider(fakes(umc=(umc_margin(),), fubon=(fubon_margin(),)))

    display = p.refresh(notional_twd=NOTIONAL)

    # equity_twd: umc 10,000 * 30 = 300,000 -> ratio 0.30; fubon 900,000 -> 0.90
    assert display.umc_ratio == pytest.approx(0.30)
    assert display.fubon_ratio == pytest.approx(0.90)
    assert display.umc_equity_twd == pytest.approx(300_000.0)
    assert display.fubon_equity_twd == pytest.approx(900_000.0)
    # uPnL: umc 500 * 30 = 15,000; fubon -20,000; combined = -5,000
    assert display.combined_upnl_twd == pytest.approx(-5_000.0)
    assert display.stale is False


def test_flat_still_reports_margin_level(monkeypatch: pytest.MonkeyPatch) -> None:
    # Flat: no positions, uPnL == 0, but equity/notional water level still shows.
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    p = provider(
        fakes(
            umc=(umc_margin(upnl=0.0),),
            fubon=(fubon_margin(upnl=0.0),),
        )
    )

    display = p.refresh(notional_twd=NOTIONAL)

    assert display.combined_upnl_twd == pytest.approx(0.0)
    assert display.umc_ratio == pytest.approx(0.30)
    assert display.fubon_ratio == pytest.approx(0.90)


def test_margin_level_tracks_notional_denominator(monkeypatch: pytest.MonkeyPatch) -> None:
    # Halving the (current-price) notional doubles the water level.
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    p = provider(fakes(umc=(umc_margin(),), fubon=(fubon_margin(),)))

    tight = p.refresh(notional_twd=NOTIONAL)
    loose = p.refresh(notional_twd=NOTIONAL / 2)

    assert loose.umc_ratio == pytest.approx(tight.umc_ratio * 2)


def test_missing_fubon_upnl_key_yields_na_pnl_but_keeps_ratios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    p = provider(
        fakes(umc=(umc_margin(),), fubon=(fubon_margin(upnl=None),))
    )

    display = p.refresh(notional_twd=NOTIONAL)

    assert display.combined_upnl_twd is None
    assert display.fubon_ratio == pytest.approx(0.90)


def test_disabled_without_env_returns_na_and_builds_no_brokers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LUX_READONLY_BROKER", raising=False)
    p = provider(fakes(umc=(umc_margin(),), fubon=(fubon_margin(),)))

    display = p.refresh(notional_twd=NOTIONAL)

    assert display == AccountDisplay(fetched_at=display.fetched_at)
    assert display.combined_upnl_twd is None
    assert display.umc_ratio is None
    assert p._brokers is None  # brokers never constructed while disabled


def test_fetch_failure_keeps_last_known_and_marks_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    p = provider(fakes(umc=(umc_margin(),), fubon=(fubon_margin(),)))

    good = p.refresh(notional_twd=NOTIONAL)
    assert good.stale is False

    # Force the next fetch to raise; the loop must not crash, values are retained.
    p._brokers = (
        FakeReadOnlyBroker(BrokerName.FUBON_CCF, fetch_error=RuntimeError("boom")),
        FakeReadOnlyBroker(BrokerName.IBKR_UMC, fetch_error=RuntimeError("boom")),
    )
    stale = p.refresh(notional_twd=NOTIONAL)

    assert stale.stale is True
    assert stale.combined_upnl_twd == pytest.approx(good.combined_upnl_twd)
    assert stale.umc_ratio == pytest.approx(good.umc_ratio)


class MarginsOnlyBroker:
    """Read-only broker that exposes the lightweight fetch_margins path only."""

    def __init__(self, broker: BrokerName, margins: tuple) -> None:
        self.broker = broker
        self._margins = margins
        self.margins_calls = 0
        self.snapshot_calls = 0

    def fetch_margins(self) -> BrokerAccountSnapshot:
        self.margins_calls += 1
        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id="X",
            fetched_at=datetime.now().astimezone(),
            margins=self._margins,
        )

    def fetch_snapshot(self) -> BrokerAccountSnapshot:  # pragma: no cover - guard
        self.snapshot_calls += 1
        raise AssertionError("account panel must not call heavy fetch_snapshot")

    def close(self) -> None:
        pass


def test_provider_prefers_lightweight_fetch_margins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    fubon = MarginsOnlyBroker(BrokerName.FUBON_CCF, (fubon_margin(),))
    umc = MarginsOnlyBroker(BrokerName.IBKR_UMC, (umc_margin(),))
    p = provider((fubon, umc))

    display = p.refresh(notional_twd=NOTIONAL)

    assert fubon.margins_calls == 1 and umc.margins_calls == 1
    assert fubon.snapshot_calls == 0 and umc.snapshot_calls == 0
    assert display.fubon_ratio == pytest.approx(0.90)
    assert display.combined_upnl_twd == pytest.approx(-5_000.0)


def test_first_fetch_failure_returns_na_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    p = provider(
        (
            FakeReadOnlyBroker(BrokerName.FUBON_CCF, fetch_error=RuntimeError("boom")),
            FakeReadOnlyBroker(BrokerName.IBKR_UMC, fetch_error=RuntimeError("boom")),
        )
    )

    display = p.refresh(notional_twd=NOTIONAL)

    assert display.stale is True
    assert display.combined_upnl_twd is None
    assert display.umc_ratio is None
