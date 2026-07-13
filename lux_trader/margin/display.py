"""Live account-panel provider: real broker uPnL + margin water level for the UI.

Display-only sibling of :mod:`lux_trader.margin.monitor`. The monitor owns the
scheduled daily/red-line *decisions*; this provider owns the per-bar *display*
numbers the terminal UI shows in place of the synthetic model pnl/equity:

- ``combined_upnl_twd`` — Binance position uPnL (USDT→TWD) + Fubon position uPnL.
- ``binance_ratio`` / ``fubon_ratio`` — 保證金水位 = ``equity_twd / notional_twd``
  where ``notional_twd`` is the *current-price* leg notional passed in by the
  engine (so the水位 shows even when flat).

Same env gate as the monitor (``LUX_READONLY_BROKER=1``); broker/API hiccups are
swallowed and the last-known values are kept (marked ``stale``) so the live loop
never dies for a display refresh.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from lux_trader.config import AppConfig
from lux_trader.margin.monitor import READONLY_BROKER_ENV, build_default_margin_brokers
from lux_trader.margin.policy import assess_venue
from lux_trader.margin.service import (
    fetch_margin_snapshot,
    raw_float,
    reading_from_snapshot,
)
from lux_trader.reconciliation import ReadOnlyBroker
from lux_trader.reconciliation.models import BrokerAccountSnapshot


# Fubon unrealized P&L key inside the raw query_margin_equity row. Confirmed
# against a live 2026-07-07 response: futures uPnL is ``fut_unrealized_pnl``
# (the row also carries ``opt_pnl`` / ``fut_realized_pnl``). The remaining names
# are defensive fallbacks in case the SDK schema shifts.
FUBON_UPNL_KEYS = (
    "fut_unrealized_pnl",
    "unrealized_pnl",
    "unrealised_pnl",
    "upnl",
    "floating_pnl",
    "未實現損益",
)


@dataclass(frozen=True)
class AccountDisplay:
    """UI-side snapshot of real account numbers. All-None means unavailable."""

    combined_upnl_twd: float | None = None
    binance_ratio: float | None = None
    fubon_ratio: float | None = None
    binance_equity_twd: float | None = None
    fubon_equity_twd: float | None = None
    stale: bool = False
    fetched_at: datetime | None = None


class AccountDisplayProvider:
    def __init__(
        self,
        config: AppConfig,
        *,
        usdttwd_rate: Callable[[], float | None],
        brokers_factory: Callable[[], tuple[ReadOnlyBroker, ReadOnlyBroker]]
        | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.usdttwd_rate = usdttwd_rate
        self._brokers_factory = brokers_factory or (
            lambda: build_default_margin_brokers(config)
        )
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._brokers: tuple[ReadOnlyBroker, ReadOnlyBroker] | None = None
        self._latest = AccountDisplay()

    @property
    def latest(self) -> AccountDisplay:
        return self._latest

    def enabled(self) -> bool:
        return os.getenv(READONLY_BROKER_ENV, "").strip() == "1"

    def ensure_brokers(self) -> tuple[ReadOnlyBroker, ReadOnlyBroker]:
        """Build the shared (fubon, binance) broker pair lazily, once."""
        if self._brokers is None:
            self._brokers = self._brokers_factory()
        return self._brokers

    def refresh(self, *, notional_twd: float) -> AccountDisplay:
        """Fetch both snapshots and recompute the display numbers.

        Called once per finalized bar. Never raises: on a broker/API failure the
        last-known values are returned with ``stale=True``.
        """
        if not self.enabled():
            self._latest = AccountDisplay(fetched_at=self.clock())
            return self._latest
        try:
            fubon_broker, binance_broker = self.ensure_brokers()
            fubon_snapshot = fetch_margin_snapshot(fubon_broker)
            binance_snapshot = fetch_margin_snapshot(binance_broker)
            self._latest = self._compute(
                binance_snapshot=binance_snapshot,
                fubon_snapshot=fubon_snapshot,
                notional_twd=notional_twd,
                rate=self.usdttwd_rate(),
            )
        except Exception:
            # A refresh must never kill the trading loop; keep the last values.
            self._latest = replace(self._latest, stale=True)
        return self._latest

    def close(self) -> None:
        if self._brokers is None:
            return
        for broker in self._brokers:
            try:
                broker.close()
            except Exception:
                pass
        self._brokers = None

    # ------------------------------------------------------------------
    def _compute(
        self,
        *,
        binance_snapshot: BrokerAccountSnapshot,
        fubon_snapshot: BrokerAccountSnapshot,
        notional_twd: float,
        rate: float | None,
    ) -> AccountDisplay:
        binance = assess_venue(
            reading_from_snapshot(binance_snapshot, "binance"),
            notional_twd=notional_twd,
            usdttwd_rate=rate,
            red_line_ratio=self.config.margin_management.binance_red_line_ratio,
            transfer_ratio=self.config.margin_management.binance_transfer_ratio,
            target_ratio=self.config.margin_management.target_ratio,
            red_line_maint_multiplier=self.config.margin_management.red_line_maint_multiplier,
            position_open=False,
            check_type="display",
        )
        fubon = assess_venue(
            reading_from_snapshot(fubon_snapshot, "fubon"),
            notional_twd=notional_twd,
            usdttwd_rate=rate,
            red_line_ratio=self.config.margin_management.fubon_red_line_ratio,
            transfer_ratio=self.config.margin_management.fubon_transfer_ratio,
            target_ratio=self.config.margin_management.target_ratio,
            red_line_maint_multiplier=self.config.margin_management.red_line_maint_multiplier,
            position_open=False,
            check_type="display",
        )
        binance_upnl = self._binance_upnl_twd(binance_snapshot, rate)
        fubon_upnl = self._fubon_upnl_twd(fubon_snapshot)
        combined = (
            binance_upnl + fubon_upnl
            if binance_upnl is not None and fubon_upnl is not None
            else None
        )
        return AccountDisplay(
            combined_upnl_twd=combined,
            binance_ratio=binance.ratio,
            fubon_ratio=fubon.ratio,
            binance_equity_twd=binance.equity_twd,
            fubon_equity_twd=fubon.equity_twd,
            stale=False,
            fetched_at=self.clock(),
        )

    @staticmethod
    def _binance_upnl_twd(
        snapshot: BrokerAccountSnapshot, rate: float | None
    ) -> float | None:
        if not snapshot.margins or rate is None:
            return None
        margin = snapshot.margins[0]
        upnl_usdt = raw_float(margin.raw, "totalUnrealizedProfit")
        if upnl_usdt is None:
            wallet = raw_float(margin.raw, "totalWalletBalance")
            if margin.equity is None or wallet is None:
                return None
            upnl_usdt = margin.equity - wallet
        return upnl_usdt * rate

    @staticmethod
    def _fubon_upnl_twd(snapshot: BrokerAccountSnapshot) -> float | None:
        if not snapshot.margins:
            return None
        return raw_float(snapshot.margins[0].raw, *FUBON_UPNL_KEYS)
