from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from lux_trader.integrations.binance.execution import BinanceTsmExecutionAdapter
from lux_trader.brokers import PaperBroker
from lux_trader.config import AppConfig
from lux_trader.core.contract_policy import ExpiryBufferContractPolicy, QffContractSelection
from lux_trader.core.calendar import live_session_status
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
from lux_trader.integrations.binance.market_data import BinanceMarketData
from lux_trader.integrations.bitopro.market_data import BitoProMarketData
from lux_trader.integrations.fubon.execution import FubonFutureExecutionAdapter
from lux_trader.integrations.fubon.market_data import FubonQffMarketData
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.taifex.downloader import TaifexQffTradeDownloader
from lux_trader.core.fees import fill_costs
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.execution.gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from lux_trader.market_data import (
    CsvQffWarmupProvider,
    LiveMinuteBarBuilder,
    LiveQuoteSet,
    OhlcvProvider,
    QFF_FORWARD_FILL_LOOKBACK,
    QffWarmupSourceReport,
    QffWarmupProvider,
    QuoteProvider,
    WarmupBuilder,
    build_qff_session_index,
    build_qff_session_warmup_index,
    build_qff_warmup_source_report,
    floor_minute,
    parse_timestamp,
    prioritized_qff_close_frame,
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

QFF_RECONNECT_GRACE_SECONDS = 10.0
QFF_RECONNECT_RETRY_SECONDS = 30.0
QFF_WATCHDOG_SECONDS = 120.0


@dataclass(frozen=True)
class QffContractResolution:
    symbol: str
    expiry: str | None
    policy_state: str
    selection: QffContractSelection | None = None


def subscribe_qff_books_if_supported(
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
            "qff_books",
            f"subscribe_failed:{type(exc).__name__}",
        )


def teardown_qff_books_if_supported(provider: object) -> None:
    teardown = getattr(provider, "teardown_books_session", None)
    if callable(teardown):
        teardown()


def restart_qff_books_if_supported(
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
        < QFF_RECONNECT_RETRY_SECONDS
    ):
        return last_restart_at
    restart = getattr(provider, "restart_books_session", None)
    if callable(restart):
        try:
            reporter.event(timestamp, "qff_books", f"restart_books_{symbol}")
            restart(symbol)
        except Exception as exc:
            reporter.warn(
                timestamp,
                "qff_books",
                f"restart_failed:{type(exc).__name__}",
            )
    else:
        unsubscribe_qff_books_if_supported(provider, symbol)
        subscribe_qff_books_if_supported(provider, symbol, reporter, timestamp)
    return timestamp


def reconnect_qff_provider_if_supported(
    provider: object,
    reporter: Any,
    timestamp: datetime,
) -> None:
    # Proactively re-login on entering a trading session so the marketdata token is
    # fresh for the whole session. The longest continuous session (~11.5h night) is
    # well within the observed token lifetime, so this avoids the overnight 401
    # without parsing error strings. No-op for providers without reconnect support.
    reconnect = getattr(provider, "reconnect", None)
    if not callable(reconnect):
        return
    timestamp = ensure_taipei(timestamp)
    try:
        reporter.event(timestamp, "qff_books", "reconnect_login")
        reconnect()
    except Exception as exc:
        reporter.warn(timestamp, "qff_books", f"reconnect_failed:{type(exc).__name__}")


def unsubscribe_qff_books_if_supported(provider: object, symbol: str) -> None:
    unsubscribe = getattr(provider, "unsubscribe_books", None)
    if callable(unsubscribe):
        unsubscribe(symbol)


def qff_book_age_seconds(quote: Any, observed_at: datetime) -> float:
    return abs((ensure_taipei(observed_at) - ensure_taipei(quote.timestamp)).total_seconds())


def qff_book_is_fresh_for_signal(
    quote: Any,
    observed_at: datetime,
    config: AppConfig,
) -> bool:
    if getattr(quote, "bid", None) is None or getattr(quote, "ask", None) is None:
        return False
    return qff_book_age_seconds(quote, observed_at) <= config.live.qff_book_stale_seconds


def initialize_contract_state(
    state: StrategyRuntimeState,
    contract: QffContractResolution,
) -> None:
    state.eligible_active_qff_symbol = contract.symbol
    state.eligible_active_qff_expiry = contract.expiry
    if state.trading_qff_symbol is None:
        state.trading_qff_symbol = contract.symbol
        state.trading_qff_expiry = contract.expiry
        state.contract_policy_state = contract.policy_state
    if state.last_warmup_symbol is None:
        state.last_warmup_symbol = state.trading_qff_symbol


def update_eligible_contract_state(
    state: StrategyRuntimeState,
    contract: QffContractResolution,
) -> None:
    state.eligible_active_qff_symbol = contract.symbol
    state.eligible_active_qff_expiry = contract.expiry


def should_switch_contract_before_processing(
    state: StrategyRuntimeState,
    contract: QffContractResolution,
) -> bool:
    if state.trading_qff_symbol == contract.symbol:
        return False
    return state.state in (StrategyState.FLAT, StrategyState.ENTRY_PENDING)


def mark_pending_contract_switch_if_needed(
    state: StrategyRuntimeState,
    contract: QffContractResolution,
) -> None:
    update_eligible_contract_state(state, contract)
    if state.trading_qff_symbol == contract.symbol:
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
    if state.trading_qff_expiry is None:
        return False
    expiry = datetime.fromisoformat(state.trading_qff_expiry).date()
    return ExpiryBufferContractPolicy(config.contract_policy).should_force_exit(
        timestamp,
        expiry,
    )


def switch_to_contract(
    store: SQLiteStore,
    config: AppConfig,
    state: StrategyRuntimeState,
    contract: QffContractResolution,
    *,
    qff_provider: QffWarmupProvider,
    tsm_provider: OhlcvProvider,
    usdttwd_provider: OhlcvProvider,
    end: datetime,
) -> tuple[str, str | None, IndicatorEngine, list[Any]]:
    state.trading_qff_symbol = contract.symbol
    state.trading_qff_expiry = contract.expiry
    state.eligible_active_qff_symbol = contract.symbol
    state.eligible_active_qff_expiry = contract.expiry
    state.pending_symbol_switch = False
    state.last_warmup_symbol = contract.symbol
    state.contract_policy_state = contract.policy_state
    indicator, seed_bars = load_or_build_live_indicator(
        store,
        config,
        qff_symbol=contract.symbol,
        qff_expiry=contract.expiry,
        policy_state=contract.policy_state,
        qff_provider=qff_provider,
        tsm_provider=tsm_provider,
        usdttwd_provider=usdttwd_provider,
        end=end,
        force_rebuild=True,
    )
    store.record_event(
        seed_bars[-1].row_index,
        seed_bars[-1].timestamp,
        "warmup_rebuilt_for_new_contract",
        "warmup rebuilt for QFF contract",
        {"qff_symbol": contract.symbol, "qff_expiry": contract.expiry},
    )
    return contract.symbol, contract.expiry, indicator, seed_bars


def resolve_qff_contract(
    config: AppConfig,
    provider: object,
    *,
    now: datetime | None = None,
) -> QffContractResolution:
    configured = config.live.qff_symbol
    if configured.lower() != "auto":
        return QffContractResolution(
            symbol=configured,
            expiry=None,
            policy_state="fixed_symbol",
        )

    fetch_candidates = getattr(provider, "fetch_candidates", None)
    if config.contract_policy.enabled and fetch_candidates is not None:
        selection = ExpiryBufferContractPolicy(config.contract_policy).select_active(
            fetch_candidates(config.live.qff_product),
            product=config.live.qff_product,
            now=now,
        )
        return QffContractResolution(
            symbol=selection.symbol,
            expiry=selection.expiry.isoformat(),
            policy_state="active",
            selection=selection,
        )

    selector = getattr(provider, "select_front_month_symbol", None)
    if selector is None:
        raise RuntimeError("qff_symbol=auto requires a provider with front-month selector")
    return QffContractResolution(
        symbol=str(selector(config.live.qff_product)),
        expiry=None,
        policy_state="front_month",
    )

