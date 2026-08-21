from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from lux_trader.config import AppConfig
from lux_trader.core.contract_policy import ExpiryBufferContractPolicy, CcfContractSelection
from lux_trader.core.calendar import is_weekend_force_exit_bar, live_session_status
from lux_trader.execution.intent import (
    ExecutionPlanType,
    PairExecutionPlan,
    pair_execution_plan_from_order_requests,
)
from lux_trader.execution import (
    ExecutionCoordinator,
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    SimulatedExecutionAdapter,
)
from lux_trader.execution.recorder import DryRunExecutionRecorder
from lux_trader.execution.price_policy import apply_live_touch_market_price_policy
from lux_trader.integrations.fubon.execution import FubonFutureExecutionAdapter
from lux_trader.integrations.fubon.contracts import normalize_fubon_order_symbol
from lux_trader.integrations.fubon.market_data import FubonCcfMarketData
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.integrations.taifex.downloader import TaifexCcfTradeDownloader
from lux_trader.core.fees import fill_costs
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.execution.gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from lux_trader.market_data import (
    CsvCcfWarmupProvider,
    LiveMinuteBarBuilder,
    LiveQuoteSet,
    OhlcvProvider,
    CCF_FORWARD_FILL_LOOKBACK,
    CcfWarmupSourceReport,
    CcfWarmupProvider,
    QuoteProvider,
    WarmupBuilder,
    build_ccf_session_index,
    build_ccf_session_warmup_index,
    build_ccf_warmup_source_report,
    floor_minute,
    parse_timestamp,
    prioritized_ccf_close_frame,
)
from lux_trader.store import SQLiteStore
from lux_trader.core.models import Direction, IndicatorSnapshot, MarketBar, StrategyAction, StrategyState
from lux_trader.reconciliation.post_trade import PostTradeReconciler
from lux_trader.execution.real_coordinator import RealExecutionCoordinator
from lux_trader.reconciliation import ReadOnlyBroker, ReconciliationReport, ReconciliationStatus
from lux_trader.core.sizing import size_position_for_direction
from lux_trader.core.strategy import PairStrategy, StrategyRuntimeState, minutes_between
from lux_trader.terminal_ui import (
    NullLiveReporter,
    compact_reason,
    compact_warning_code,
)
from lux_trader.core.tradable_spread import TradableSpreadSnapshot, estimate_tradable_spreads
from lux_trader.core.time import ensure_taipei

CCF_RECONNECT_GRACE_SECONDS = 10.0
CCF_RECONNECT_RETRY_SECONDS = 30.0
CCF_WATCHDOG_SECONDS = 120.0


@dataclass(frozen=True)
class CcfContractResolution:
    symbol: str
    expiry: str | None
    policy_state: str
    selection: CcfContractSelection | None = None


def subscribe_ccf_books_if_supported(
    provider: object,
    symbol: str,
    reporter: Any,
    timestamp: datetime,
) -> None:
    subscribe = getattr(provider, "ensure_books_subscription", None)
    if not callable(subscribe):
        return
    try:
        reporter.event(timestamp, "startup", f"subscribe_books_{symbol}")
        subscribe(symbol)
    except Exception as exc:
        reporter.warn(
            timestamp,
            "ccf_books",
            f"subscribe_failed:{type(exc).__name__}",
        )


def teardown_ccf_books_if_supported(provider: object) -> None:
    teardown = getattr(provider, "teardown_books_session", None)
    if callable(teardown):
        teardown()


def restart_ccf_books_if_supported(
    provider: object,
    symbol: str,
    reporter: Any,
    timestamp: datetime,
    *,
    last_restart_at: datetime | None,
) -> datetime:
    timestamp = ensure_taipei(timestamp)
    if (
        last_restart_at is not None
        and (timestamp - ensure_taipei(last_restart_at)).total_seconds()
        < CCF_RECONNECT_RETRY_SECONDS
    ):
        return last_restart_at
    restart = getattr(provider, "restart_books_session", None)
    if callable(restart):
        try:
            reporter.event(timestamp, "ccf_books", f"restart_books_{symbol}")
            restart(symbol)
        except Exception as exc:
            reporter.warn(
                timestamp,
                "ccf_books",
                f"restart_failed:{type(exc).__name__}",
            )
    else:
        unsubscribe_ccf_books_if_supported(provider, symbol)
        subscribe_ccf_books_if_supported(provider, symbol, reporter, timestamp)
    return timestamp


def reconnect_provider_if_supported(
    provider: object,
    reporter: Any,
    timestamp: datetime,
    *,
    label: str,
) -> None:
    # Proactively re-establish the venue link on entering a trading session, so
    # the session starts on something this process built rather than on
    # whatever survived the idle gap. No-op for providers without reconnect
    # support, and never a reason to stop: a failed reconnect is reported and
    # the normal per-quote path still gets its chance.
    reconnect = getattr(provider, "reconnect", None)
    if not callable(reconnect):
        return
    timestamp = ensure_taipei(timestamp)
    try:
        reporter.event(timestamp, label, "reconnect_login")
        reconnect()
    except Exception as exc:
        reporter.warn(timestamp, label, f"reconnect_failed:{type(exc).__name__}")


def reconnect_ccf_provider_if_supported(
    provider: object,
    reporter: Any,
    timestamp: datetime,
) -> None:
    # The marketdata token is fresh for the whole session afterwards. The
    # longest continuous session (~11.5h night) is well within the observed
    # token lifetime, so this avoids the overnight 401 without parsing error
    # strings.
    reconnect_provider_if_supported(
        provider, reporter, timestamp, label="ccf_books"
    )


def reconnect_umc_provider_if_supported(
    provider: object,
    reporter: Any,
    timestamp: datetime,
) -> None:
    # The same treatment CCF has always had, for the venue that had none.
    #
    # IBKR needs it for a different reason than Fubon: not an expiring token,
    # but a TCP socket left idle across the 17.5-hour gap between sessions and
    # through IBKR's nightly reset. On 2026-08-21 that socket was closed by the
    # peer during the night, still reported connected, and cost the first quote
    # of the session -- one fetch_umc failure, and a skipped minute after it
    # while the two legs' timestamps drifted back together.
    reconnect_provider_if_supported(
        provider, reporter, timestamp, label="umc_quote"
    )


def unsubscribe_ccf_books_if_supported(provider: object, symbol: str) -> None:
    unsubscribe = getattr(provider, "unsubscribe_books", None)
    if callable(unsubscribe):
        unsubscribe(symbol)


def ccf_book_age_seconds(quote: Any, observed_at: datetime) -> float:
    return abs((ensure_taipei(observed_at) - ensure_taipei(quote.timestamp)).total_seconds())


def ccf_book_is_fresh_for_signal(
    quote: Any,
    observed_at: datetime,
    config: AppConfig,
) -> bool:
    if getattr(quote, "bid", None) is None or getattr(quote, "ask", None) is None:
        return False
    return ccf_book_age_seconds(quote, observed_at) <= config.live.ccf_book_stale_seconds


def initialize_contract_state(
    state: StrategyRuntimeState,
    contract: CcfContractResolution,
) -> None:
    state.eligible_active_ccf_symbol = contract.symbol
    state.eligible_active_ccf_expiry = contract.expiry
    if state.trading_ccf_symbol is None:
        state.trading_ccf_symbol = contract.symbol
        state.trading_ccf_expiry = contract.expiry
        state.contract_policy_state = contract.policy_state
    if state.last_warmup_symbol is None:
        state.last_warmup_symbol = state.trading_ccf_symbol


def update_eligible_contract_state(
    state: StrategyRuntimeState,
    contract: CcfContractResolution,
) -> None:
    state.eligible_active_ccf_symbol = contract.symbol
    state.eligible_active_ccf_expiry = contract.expiry


def should_switch_contract_before_processing(
    state: StrategyRuntimeState,
    contract: CcfContractResolution,
) -> bool:
    if state.trading_ccf_symbol == contract.symbol:
        return False
    return state.state in (StrategyState.FLAT, StrategyState.ENTRY_PENDING)


def mark_pending_contract_switch_if_needed(
    state: StrategyRuntimeState,
    contract: CcfContractResolution,
) -> None:
    update_eligible_contract_state(state, contract)
    if state.trading_ccf_symbol == contract.symbol:
        state.pending_symbol_switch = False
        state.contract_policy_state = "active"
        return
    if state.state in (StrategyState.OPEN, StrategyState.EXIT_PENDING):
        state.pending_symbol_switch = True
        state.contract_policy_state = "pending_symbol_switch"


def cancel_entry_pending_for_contract_switch(state: StrategyRuntimeState) -> None:
    state.state = StrategyState.FLAT
    state.candidate_direction = None
    state.candidate_idx = -1
    state.candidate_time = None
    state.candidate_zscore = None


def should_force_exit_for_contract_policy(
    config: AppConfig,
    state: StrategyRuntimeState,
    timestamp: datetime,
) -> bool:
    if not config.contract_policy.enabled:
        return False
    if state.position_direction is None:
        return False
    if state.trading_ccf_expiry is None:
        return False
    expiry = datetime.fromisoformat(state.trading_ccf_expiry).date()
    return ExpiryBufferContractPolicy(config.contract_policy).should_force_exit(
        timestamp,
        expiry,
    )


def should_force_exit_for_weekend(
    config: AppConfig,
    state: StrategyRuntimeState,
    timestamp: datetime,
) -> bool:
    # Only an open position is force-closed, mirroring the contract-policy guard so
    # a flat strategy never routes an exit while flat (which would ERROR the
    # dry-run / live-execute coordinators).
    if state.position_direction is None:
        return False
    return is_weekend_force_exit_bar(
        timestamp,
        config.trading_calendar.closed_dates,
        weekend_policy=config.strategy.weekend_policy,
    )


def resolve_force_exit_reason(
    config: AppConfig,
    state: StrategyRuntimeState,
    timestamp: datetime,
) -> str | None:
    """The force-exit reason for this bar, or None. Expiry rollover takes
    precedence over the weekend/session-end flatten."""
    if should_force_exit_for_contract_policy(config, state, timestamp):
        return "rollover_force_exit"
    if should_force_exit_for_weekend(config, state, timestamp):
        return "weekend_force_exit"
    return None


def switch_to_contract(
    store: SQLiteStore,
    config: AppConfig,
    state: StrategyRuntimeState,
    contract: CcfContractResolution,
    *,
    ccf_provider: CcfWarmupProvider,
    umc_provider: OhlcvProvider,
    usd_twd_provider: OhlcvProvider,
    end: datetime,
) -> tuple[str, str | None, IndicatorEngine, list[Any]]:
    # Imported here, not at module scope: warmup imports resolve_ccf_contract
    # from THIS module, so a top-level import closes the cycle and neither
    # module loads. The name was simply missing before -- this function is only
    # reached when a rollover completes while flat, which first happened live on
    # 2026-08-17, and it raised NameError with the position already closed.
    from lux_trader.runtime.live.warmup import load_or_build_live_indicator

    state.trading_ccf_symbol = contract.symbol
    state.trading_ccf_expiry = contract.expiry
    state.eligible_active_ccf_symbol = contract.symbol
    state.eligible_active_ccf_expiry = contract.expiry
    state.pending_symbol_switch = False
    state.last_warmup_symbol = contract.symbol
    state.contract_policy_state = contract.policy_state
    indicator, seed_bars = load_or_build_live_indicator(
        store,
        config,
        ccf_symbol=contract.symbol,
        ccf_expiry=contract.expiry,
        policy_state=contract.policy_state,
        ccf_provider=ccf_provider,
        umc_provider=umc_provider,
        usd_twd_provider=usd_twd_provider,
        end=end,
        force_rebuild=True,
    )
    store.record_event(
        seed_bars[-1].row_index,
        seed_bars[-1].timestamp,
        "warmup_rebuilt_for_new_contract",
        "warmup rebuilt for CCF contract",
        {
            "ccf_symbol": contract.symbol,
            "ccf_expiry": contract.expiry,
            "start_timestamp": seed_bars[0].timestamp.isoformat(),
            "end_timestamp": seed_bars[-1].timestamp.isoformat(),
            "requested_end": end.isoformat(),
        },
    )
    return contract.symbol, contract.expiry, indicator, seed_bars


def resolve_ccf_contract(
    config: AppConfig,
    provider: object,
    *,
    now: datetime | None = None,
) -> CcfContractResolution:
    configured = config.live.ccf_symbol
    if configured.lower() != "auto":
        symbol = normalize_fubon_order_symbol(
            configured,
            product=config.live.ccf_product,
            reference_date=ensure_taipei(now).date() if now is not None else None,
        )
        return CcfContractResolution(
            symbol=symbol,
            expiry=None,
            policy_state="fixed_symbol",
        )

    fetch_candidates = getattr(provider, "fetch_candidates", None)
    if config.contract_policy.enabled and fetch_candidates is not None:
        selection = ExpiryBufferContractPolicy(config.contract_policy).select_active(
            fetch_candidates(config.live.ccf_product),
            product=config.live.ccf_product,
            now=now,
        )
        symbol = normalize_fubon_order_symbol(
            selection.symbol,
            product=config.live.ccf_product,
            expiry=selection.expiry,
            reference_date=ensure_taipei(now).date() if now is not None else None,
        )
        return CcfContractResolution(
            symbol=symbol,
            expiry=selection.expiry.isoformat(),
            policy_state="active",
            selection=selection,
        )

    selector = getattr(provider, "select_front_month_symbol", None)
    if selector is None:
        raise RuntimeError("ccf_symbol=auto requires a provider with front-month selector")
    selected_symbol = str(selector(config.live.ccf_product))
    symbol = normalize_fubon_order_symbol(
        selected_symbol,
        product=config.live.ccf_product,
        reference_date=ensure_taipei(now).date() if now is not None else None,
    )
    return CcfContractResolution(
        symbol=symbol,
        expiry=None,
        policy_state="front_month",
    )
