"""Scheduling wrapper for margin checks inside the live loop.

Owns the "when" (daily at check_time Mon-Fri, red-line every N minutes while a
position is open) and the session state (already-checked guard, retry backoff,
lazy read-only brokers). The "what" lives in policy/service.

Real read-only brokers touch private broker APIs, so the monitor stays inert
unless ``[margin_management] enabled=true`` AND ``LUX_READONLY_BROKER=1``; a
missing env gate is reported once and disables the monitor for the session.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable

from lux_trader.config import AppConfig
from lux_trader.core.contract_policy import parse_hhmm
from lux_trader.core.models import StrategyState
from lux_trader.core.time import TAIPEI_TZ, ensure_taipei
from lux_trader.margin.service import (
    MarginCheckService,
    record_and_report_decision,
    resolve_margin_leg_notional_twd,
)
from lux_trader.reconciliation import ReadOnlyBroker


READONLY_BROKER_ENV = "LUX_READONLY_BROKER"

POSITION_OPEN_STATES = (StrategyState.OPEN, StrategyState.EXIT_PENDING)


def build_default_margin_brokers(
    config: AppConfig,
) -> tuple[ReadOnlyBroker, ReadOnlyBroker]:
    from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
    from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker

    return (
        FubonReadOnlyBroker(config.live.fubon_env_path),
        BinanceReadOnlyBroker(
            config.live.binance_symbol,
            config.live.fubon_env_path,
        ),
    )


class MarginMonitor:
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
        self.enabled = bool(config.margin_management.enabled)
        self.usdttwd_rate = usdttwd_rate
        self.brokers_factory = brokers_factory or (
            lambda: build_default_margin_brokers(config)
        )
        self.clock = clock
        hour, minute = parse_hhmm(config.margin_management.check_time)
        self.check_time = dt_time(hour=hour, minute=minute)
        self.retry_interval = timedelta(
            minutes=max(1, config.margin_management.red_line_interval_minutes)
        )
        self._env_gate_checked = False
        self._brokers: tuple[ReadOnlyBroker, ReadOnlyBroker] | None = None
        self._service: MarginCheckService | None = None
        self._last_daily_date: date | None = None
        self._last_daily_loaded = False
        self._last_red_line_at: datetime | None = None
        self._retry_at: datetime | None = None

    # ------------------------------------------------------------------
    def maybe_run(
        self,
        observed_at: datetime,
        *,
        strategy_state: Any,
        store: Any,
        reporter: Any,
    ) -> None:
        if not self.enabled:
            return
        observed_at = ensure_taipei(observed_at)
        if not self._check_env_gate(observed_at, reporter):
            return
        if self._retry_at is not None and observed_at < self._retry_at:
            return

        position_open = (
            getattr(strategy_state, "state", None) in POSITION_OPEN_STATES
        )
        if self._daily_due(observed_at, store):
            self._run(
                observed_at,
                check_type="daily",
                position_open=position_open,
                store=store,
                reporter=reporter,
            )
        elif position_open and self._red_line_due(observed_at):
            self._run(
                observed_at,
                check_type="red_line",
                position_open=True,
                store=store,
                reporter=reporter,
            )

    def close(self) -> None:
        if self._brokers is None:
            return
        for broker in self._brokers:
            try:
                broker.close()
            except Exception:
                pass
        self._brokers = None
        self._service = None

    # ------------------------------------------------------------------
    def _check_env_gate(self, observed_at: datetime, reporter: Any) -> bool:
        if self._env_gate_checked:
            return self.enabled
        self._env_gate_checked = True
        if os.getenv(READONLY_BROKER_ENV, "").strip() != "1":
            self.enabled = False
            reporter.warn(
                observed_at,
                "margin_check_disabled",
                f"set {READONLY_BROKER_ENV}=1 to enable margin checks",
            )
        return self.enabled

    def _daily_due(self, observed_at: datetime, store: Any) -> bool:
        if observed_at.weekday() >= 5:
            return False
        if observed_at.timetz().replace(tzinfo=None) < self.check_time:
            return False
        if not self._last_daily_loaded:
            self._last_daily_loaded = True
            last = store.load_last_margin_check("daily")
            if last is not None:
                try:
                    self._last_daily_date = datetime.fromisoformat(
                        str(last["checked_at"])
                    ).astimezone(TAIPEI_TZ).date()
                except (KeyError, ValueError):
                    self._last_daily_date = None
        return self._last_daily_date != observed_at.date()

    def _red_line_due(self, observed_at: datetime) -> bool:
        interval = timedelta(
            minutes=max(1, self.config.margin_management.red_line_interval_minutes)
        )
        return (
            self._last_red_line_at is None
            or observed_at - self._last_red_line_at >= interval
        )

    def _run(
        self,
        observed_at: datetime,
        *,
        check_type: str,
        position_open: bool,
        store: Any,
        reporter: Any,
    ) -> None:
        try:
            service = self._ensure_service()
            decision = service.run_check(
                check_type=check_type,
                position_open=position_open,
                leg_notional_twd=resolve_margin_leg_notional_twd(
                    self.config,
                    store,
                ),
                checked_at=observed_at,
            )
            record_and_report_decision(decision, store=store, reporter=reporter)
            store.commit()
        except Exception as exc:
            # Broker/API hiccups must not kill the live loop; retry after the
            # backoff interval without marking the check as done.
            reporter.warn(
                observed_at,
                "margin_check_failed",
                f"{type(exc).__name__}: {exc}",
            )
            self._retry_at = observed_at + self.retry_interval
            return
        self._retry_at = None
        if check_type == "daily":
            self._last_daily_date = observed_at.date()
        # A daily check also covers the red-line question for this interval.
        self._last_red_line_at = observed_at

    def _ensure_service(self) -> MarginCheckService:
        if self._service is None:
            self._brokers = self.brokers_factory()
            self._service = MarginCheckService(
                self.config,
                brokers=self._brokers,
                usdttwd_rate=self.usdttwd_rate,
                clock=self.clock,
            )
        return self._service
