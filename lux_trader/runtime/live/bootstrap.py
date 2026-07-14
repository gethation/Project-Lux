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

from lux_trader.runtime.live.contracts import (
    initialize_contract_state,
    resolve_qff_contract,
    subscribe_qff_books_if_supported,
)
from lux_trader.runtime.live.warmup import load_or_build_live_indicator


@dataclass(frozen=True)
class WindowsTimeSyncResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class LiveProviderSet:
    qff: QuoteProvider | FubonQffMarketData
    tsm: QuoteProvider
    usdttwd: QuoteProvider
    close_qff_on_exit: bool


@dataclass(frozen=True)
class LiveRuntimeContext:
    started_at: datetime
    qff_provider: QuoteProvider | FubonQffMarketData
    tsm_provider: QuoteProvider
    usdttwd_provider: QuoteProvider
    qff_provider_to_close: Any | None
    qff_symbol: str
    qff_expiry: str | None
    strategy: PairStrategy
    indicator: IndicatorEngine
    seed_bars: list[MarketBar]
    builder: LiveMinuteBarBuilder
    next_row_index: int


def open_live_quote_providers(
    config: AppConfig,
    *,
    qff_provider: QuoteProvider | FubonQffMarketData | None,
    tsm_provider: QuoteProvider | None,
    usdttwd_provider: QuoteProvider | None,
    reporter: Any,
    started_at: datetime,
) -> LiveProviderSet:
    reporter.event(started_at, "startup", "init_fubon")
    qff = qff_provider or FubonQffMarketData(config.live.fubon_env_path)
    reporter.event(started_at, "startup", "init_binance")
    tsm = tsm_provider or BinanceMarketData()
    reporter.event(started_at, "startup", "init_bitopro")
    usdttwd = usdttwd_provider or BitoProMarketData()
    return LiveProviderSet(
        qff=qff,
        tsm=tsm,
        usdttwd=usdttwd,
        close_qff_on_exit=qff_provider is None,
    )


def load_or_create_strategy_state(
    store: SQLiteStore,
    *,
    resume: bool,
    config: AppConfig,
) -> StrategyRuntimeState:
    resume_state = store.load_resume_state() if resume else None
    if resume_state is not None:
        return resume_state.strategy
    return StrategyRuntimeState(
        running_max_equity=config.strategy.initial_capital_twd
    )


def build_live_strategy(
    config: AppConfig,
    strategy_state: StrategyRuntimeState,
) -> PairStrategy:
    return PairStrategy(
        config.strategy,
        config.fees,
        PaperBroker(),
        state=strategy_state,
        tsm_symbol=config.live.binance_symbol,
    )


def build_live_minute_builder(
    config: AppConfig,
    seed_bars: list[MarketBar],
) -> LiveMinuteBarBuilder:
    builder = LiveMinuteBarBuilder(
        stale_seconds=config.live.stale_seconds,
        max_leg_timestamp_skew_seconds=(
            config.live.max_leg_timestamp_skew_seconds
        ),
        closed_dates=config.trading_calendar.closed_dates,
    )
    builder.last_qff_close = seed_bars[-1].qff_close_filled
    return builder


def close_provider_quietly(provider: Any | None) -> None:
    if provider is None:
        return
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def run_windows_time_sync(timeout_seconds: float) -> WindowsTimeSyncResult:
    try:
        result = subprocess.run(
            ["w32tm", "/resync", "/force"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return WindowsTimeSyncResult(False, "timeout")
    except FileNotFoundError:
        return WindowsTimeSyncResult(False, "not_found")
    except Exception as exc:
        return WindowsTimeSyncResult(False, type(exc).__name__)
    if result.returncode == 0:
        return WindowsTimeSyncResult(True, "ok")
    return WindowsTimeSyncResult(False, f"exit_{result.returncode}")


def fetch_binance_usdm_market_time(symbol: str) -> datetime:
    import ccxt

    exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 15_000})
    errors: list[str] = []
    try:
        try:
            server_time = exchange.fetch_time()
            if server_time is not None:
                return parse_timestamp(server_time)
        except Exception as exc:
            errors.append(f"fetch_time:{type(exc).__name__}")

        try:
            exchange.load_markets()
            order_book = dict(exchange.fetch_order_book(symbol, limit=1) or {})
            timestamp = order_book.get("timestamp")
            if timestamp is not None:
                return parse_timestamp(timestamp)
            errors.append("order_book:no_timestamp")
        except Exception as exc:
            errors.append(f"order_book:{type(exc).__name__}")
    finally:
        close = getattr(exchange, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    raise RuntimeError(";".join(errors) or "market_time_unavailable")


def clock_skew_seconds(local_now: datetime, market_now: datetime) -> float:
    return abs((ensure_taipei(local_now) - ensure_taipei(market_now)).total_seconds())


def run_live_startup_preflight(
    config: AppConfig,
    reporter: Any,
    clock: Callable[[], datetime],
    *,
    platform_name: str = sys.platform,
    sync_runner: Callable[[float], WindowsTimeSyncResult] = run_windows_time_sync,
    market_time_probe: Callable[[str], datetime] = fetch_binance_usdm_market_time,
) -> None:
    local_now = ensure_taipei(clock())
    if platform_name.startswith("win") and config.live.sync_windows_time_on_startup:
        reporter.event(local_now, "startup", "sync_windows_time")
        sync_result = sync_runner(config.live.windows_time_sync_timeout_seconds)
        if not sync_result.ok:
            reporter.warn(
                ensure_taipei(clock()),
                "windows_time_sync",
                f"resync_failed:{sync_result.detail}",
            )

    local_now = ensure_taipei(clock())
    try:
        market_now = ensure_taipei(market_time_probe(config.live.binance_symbol))
    except Exception as exc:
        reporter.error(
            local_now,
            f"clock_skew unavailable:{type(exc).__name__}",
        )
        raise RuntimeError("Unable to verify market clock skew") from exc

    skew = clock_skew_seconds(local_now, market_now)
    if skew > config.live.clock_skew_fail_seconds:
        reporter.error(
            local_now,
            "clock_skew "
            f"local={local_now.isoformat()} "
            f"market={market_now.isoformat()} "
            f"skew={skew:.3f}s",
        )
        raise RuntimeError(
            "Clock skew exceeds limit: "
            f"local={local_now.isoformat()} "
            f"market={market_now.isoformat()} "
            f"skew={skew:.3f}s "
            f"limit={config.live.clock_skew_fail_seconds:.3f}s"
        )
    reporter.event(local_now, "startup", f"clock_ok skew={skew:.3f}s")


def warn_market_fetch_failure_once_per_minute(
    reporter: Any,
    observed_at: datetime,
    source: str,
    exc: Exception,
    last_warning_minute: dict[str, datetime],
) -> None:
    warning_minute = floor_minute(observed_at)
    if last_warning_minute.get(source) == warning_minute:
        return
    reporter.warn(
        observed_at,
        f"fetch_{source}",
        f"failed:{type(exc).__name__}",
    )
    last_warning_minute[source] = warning_minute


def fetch_quote_or_cached(
    provider: QuoteProvider | FubonQffMarketData,
    symbol: str,
    source: str,
    cache: dict[str, Any],
    reporter: Any,
    observed_at: datetime,
    last_warning_minute: dict[str, datetime],
) -> Any | None:
    try:
        quote = provider.fetch_quote(symbol)
    except Exception as exc:
        warn_market_fetch_failure_once_per_minute(
            reporter,
            observed_at,
            source,
            exc,
            last_warning_minute,
        )
        return cache.get(source)
    cache[source] = quote
    return quote


def prepare_live_runtime(
    *,
    config: AppConfig,
    store: SQLiteStore,
    resume: bool,
    skip_warmup: bool,
    qff_provider: QuoteProvider | FubonQffMarketData | None,
    tsm_provider: QuoteProvider | None,
    usdttwd_provider: QuoteProvider | None,
    reporter: Any,
    started_at: datetime,
    auto_warmup_context: str,
) -> LiveRuntimeContext:
    reporter.event(started_at, "startup", "store_ready")
    providers = open_live_quote_providers(
        config,
        qff_provider=qff_provider,
        tsm_provider=tsm_provider,
        usdttwd_provider=usdttwd_provider,
        reporter=reporter,
        started_at=started_at,
    )
    qff_provider = providers.qff
    tsm_provider = providers.tsm
    usdttwd_provider = providers.usdttwd
    qff_provider_to_close = (
        qff_provider if providers.close_qff_on_exit else None
    )
    reporter.event(started_at, "startup", "resolve_qff")
    initial_contract = resolve_qff_contract(
        config,
        qff_provider,
        now=started_at,
    )
    reporter.event(started_at, "startup", f"qff={initial_contract.symbol}")

    strategy_state = load_or_create_strategy_state(
        store,
        resume=resume,
        config=config,
    )
    initialize_contract_state(strategy_state, initial_contract)
    qff_symbol = strategy_state.trading_qff_symbol or initial_contract.symbol
    qff_expiry = strategy_state.trading_qff_expiry or initial_contract.expiry
    subscribe_qff_books_if_supported(
        qff_provider,
        qff_symbol,
        reporter,
        started_at,
    )

    indicator, seed_bars = load_or_build_live_indicator(
        store,
        config,
        qff_symbol=qff_symbol,
        qff_expiry=qff_expiry,
        policy_state=strategy_state.contract_policy_state or "active",
        qff_provider=qff_provider,
        tsm_provider=tsm_provider,
        usdttwd_provider=usdttwd_provider,
        end=started_at,
        # A resumed strategy keeps its persisted position/open-trade/PnL state,
        # but its rolling indicator must reflect the latest completed market
        # data.  Do not accept an old cached seed merely because it has enough
        # rows; rebuild the same fresh warmup snapshot used on first startup.
        force_rebuild=resume,
        allow_rebuild=not skip_warmup,
        reporter=reporter,
        auto_warmup_context=auto_warmup_context,
    )
    reporter.event(started_at, "startup", f"seed_ready_{len(seed_bars)}")
    return LiveRuntimeContext(
        started_at=started_at,
        qff_provider=qff_provider,
        tsm_provider=tsm_provider,
        usdttwd_provider=usdttwd_provider,
        qff_provider_to_close=qff_provider_to_close,
        qff_symbol=qff_symbol,
        qff_expiry=qff_expiry,
        strategy=build_live_strategy(config, strategy_state),
        indicator=indicator,
        seed_bars=seed_bars,
        builder=build_live_minute_builder(config, seed_bars),
        next_row_index=store.latest_bar_row_index() + 1,
    )

