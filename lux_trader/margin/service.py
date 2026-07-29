"""Margin check service: fetch broker snapshots, evaluate policy, record, report.

Semi-automatic by design — this service never places orders or initiates
transfers; it computes guidance and surfaces it through the reporter (terminal
UI) and the ``margin_checks`` audit table.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any, Callable

from lux_trader.config import AppConfig
from lux_trader.margin.policy import (
    MarginDecision,
    MarginReading,
    evaluate_margin_policy,
)
from lux_trader.reconciliation import ReadOnlyBroker
from lux_trader.reconciliation.models import BrokerAccountSnapshot


def raw_float(raw: dict[str, Any] | None, *names: str) -> float | None:
    if not raw:
        return None
    for name in names:
        value = raw.get(name)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def reading_from_snapshot(snapshot: BrokerAccountSnapshot, venue: str) -> MarginReading:
    if not snapshot.margins:
        return MarginReading(
            venue=venue,
            equity=None,
            maint_margin=None,
            currency="TWD" if venue == "fubon" else "USD",
            fetched_at=snapshot.fetched_at,
        )
    margin = snapshot.margins[0]
    if venue == "ibkr":
        maint = raw_float(margin.raw, "MaintMarginReq")
    else:
        maint = raw_float(margin.raw, "maintenance_margin", "maintenanceMargin")
    return MarginReading(
        venue=venue,
        equity=margin.equity,
        maint_margin=maint,
        currency=margin.currency,
        fetched_at=snapshot.fetched_at,
    )


def fetch_margin_snapshot(broker: ReadOnlyBroker) -> BrokerAccountSnapshot:
    """Fetch just the margin/equity data a margin check or the UI panel needs.

    Prefers the broker's lightweight ``fetch_margins`` (one accounting/balance
    call) over the full ``fetch_snapshot`` — the margin policy only reads
    ``snapshot.margins`` (equity + maintenance margin), and the extra Fubon
    ``query_single_position`` query in the full snapshot is rate-limited
    (業務系統流量控管) under frequent polling. Test doubles that only implement
    ``fetch_snapshot`` still work via the fallback.
    """
    fetch_margins = getattr(broker, "fetch_margins", None)
    if callable(fetch_margins):
        return fetch_margins()
    return broker.fetch_snapshot()


def resolve_margin_leg_notional_twd(config: AppConfig, store: Any | None = None) -> float | None:
    ccf_lots = config.strategy.ccf_lots
    if ccf_lots is None:
        return None
    price = None
    if store is not None:
        loader = getattr(store, "load_latest_ccf_close_filled", None)
        if callable(loader):
            ccf_symbol = config.live.ccf_symbol
            price = None if ccf_symbol == "auto" else loader(ccf_symbol=ccf_symbol)
            if price is None:
                price = loader()
    if price is None or price <= 0:
        return None
    return ccf_lots * config.fees.ccf_contract_multiplier * price


class MarginCheckService:
    """Runs daily / red-line margin checks against read-only brokers.

    ``brokers`` order matches helpers.build_real_readonly_brokers: (fubon,
    umc). ``usd_twd_rate`` is a callable returning the current USD/TWD
    price (injected: live loop passes the latest polled quote; the CLI fetches
    one from the FX provider).
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        brokers: tuple[ReadOnlyBroker, ReadOnlyBroker],
        usd_twd_rate: Callable[[], float | None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.fubon_broker, self.umc_broker = brokers
        self.usd_twd_rate = usd_twd_rate
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.last_umc_snapshot: BrokerAccountSnapshot | None = None

    def run_check(
        self,
        *,
        check_type: str,
        position_open: bool,
        leg_notional_twd: float | None = None,
        checked_at: datetime | None = None,
    ) -> MarginDecision:
        checked_at = checked_at or self.clock()
        fubon_snapshot = fetch_margin_snapshot(self.fubon_broker)
        umc_snapshot = fetch_margin_snapshot(self.umc_broker)
        # Kept for the caller's position-drift check. IBKR has no lightweight
        # fetch_margins, so this is already the FULL snapshot with positions in
        # it -- the comparison costs nothing extra. Fubon's is deliberately not
        # exposed: its snapshot here is margins-only by design, because
        # query_single_position is rate-limited under frequent polling.
        self.last_umc_snapshot = umc_snapshot
        margin_config = self.config.margin_management
        if leg_notional_twd is not None and leg_notional_twd > 0:
            margin_config = replace(margin_config, leg_notional_twd=leg_notional_twd)
        return evaluate_margin_policy(
            umc=reading_from_snapshot(umc_snapshot, "ibkr"),
            fubon=reading_from_snapshot(fubon_snapshot, "fubon"),
            config=margin_config,
            leg_notional_twd=self.config.strategy.leg_notional_twd,
            usd_twd_rate=self.usd_twd_rate(),
            position_open=position_open,
            checked_at=checked_at,
            check_type=check_type,
        )


def record_and_report_decision(
    decision: MarginDecision,
    *,
    store: Any,
    reporter: Any,
) -> None:
    """Persist the decision and route it to the reporter by severity."""
    store.record_margin_check(decision)
    if decision.level == "red_line":
        reporter.error(decision.checked_at, f"margin_red_line {decision.guidance}")
        reporter.warn(decision.checked_at, "margin_red_line", decision.guidance)
    elif decision.level == "transfer":
        reporter.warn(
            decision.checked_at, "margin_transfer_required", decision.guidance
        )
    else:
        reporter.event(decision.checked_at, "margin_check", decision.guidance)
