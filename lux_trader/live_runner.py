from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .binance_execution import BinanceTsmExecutionAdapter
from .brokers import PaperBroker
from .config import AppConfig
from .contract_policy import ExpiryBufferContractPolicy, QffContractSelection
from .calendar import live_session_status
from .execution_intent import (
    ExecutionPlanType,
    PairExecutionPlan,
    pair_execution_plan_from_order_requests,
)
from .execution import (
    ExecutionCoordinator,
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    SimulatedExecutionAdapter,
)
from .execution_recorder import DryRunExecutionRecorder
from .execution_price_policy import apply_live_touch_market_price_policy
from .fubon_execution import FubonFutureExecutionAdapter
from .fees import fill_costs
from .indicator import IndicatorEngine
from .live_execution_gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from .live_market_data import (
    CcxtTickerMarketData,
    CsvQffWarmupProvider,
    FubonQffMarketData,
    LiveMinuteBarBuilder,
    LiveQuoteSet,
    OhlcvProvider,
    QFF_FORWARD_FILL_LOOKBACK,
    QffWarmupSourceReport,
    QffWarmupProvider,
    TaifexQffTradeDownloader,
    QuoteProvider,
    WarmupBuilder,
    build_qff_warmup_source_report,
    ensure_taipei,
    floor_minute,
)
from .store import SQLiteStore
from .models import Direction, IndicatorSnapshot, MarketBar, StrategyAction, StrategyState
from .real_execution import RealExecutionCoordinator
from .sizing import size_position_for_direction
from .strategy import PairStrategy, StrategyRuntimeState, minutes_between
from .terminal_ui import (
    NullLiveReporter,
    compact_reason,
    compact_warning_code,
)
from .tradable_spread import TradableSpreadSnapshot, estimate_tradable_spreads


@dataclass(frozen=True)
class WarmupResult:
    bars_written: int
    qff_symbol: str
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class QffContractResolution:
    symbol: str
    expiry: str | None
    policy_state: str
    selection: QffContractSelection | None = None


@dataclass(frozen=True)
class QffWarmupCheckResult:
    qff_symbol: str
    qff_expiry: str | None
    contract_policy_state: str
    start: datetime
    end: datetime
    qff_fetch_start: datetime
    report: QffWarmupSourceReport
    output_csv: str | None


@dataclass(frozen=True)
class LivePaperResult:
    iterations: int
    bars_processed: int
    skipped_minutes: int
    qff_symbol: str


@dataclass(frozen=True)
class LiveDryRunResult:
    iterations: int
    bars_processed: int
    skipped_minutes: int
    plans_recorded: int
    qff_symbol: str


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


class WarmupRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        qff_provider: FubonQffMarketData | None = None,
        qff_fallback_provider: QffWarmupProvider | None = None,
        tsm_provider: OhlcvProvider | None = None,
        usdttwd_provider: OhlcvProvider | None = None,
    ) -> None:
        self.config = config
        self.qff_provider = qff_provider
        self.qff_fallback_provider = qff_fallback_provider
        self.tsm_provider = tsm_provider
        self.usdttwd_provider = usdttwd_provider

    def run(
        self,
        *,
        reset_store: bool = False,
        end: datetime | None = None,
    ) -> WarmupResult:
        if self.config.safety.allow_live_order:
            raise RuntimeError("Refusing warmup-live with allow_live_order=true")
        store = SQLiteStore(self.config.store_path)
        try:
            if reset_store:
                store.reset()
            store.initialize()
            qff_provider = self.qff_provider or FubonQffMarketData(
                self.config.live.fubon_env_path
            )
            contract = resolve_qff_contract(self.config, qff_provider)
            fallback = self.qff_fallback_provider
            if fallback is None and self.config.live.taifex_use_network:
                fallback = TaifexQffTradeDownloader(self.config.live.taifex_cache_dir)
            elif fallback is None and self.config.live.taifex_qff_1m_csv is not None:
                fallback = CsvQffWarmupProvider(self.config.live.taifex_qff_1m_csv)
            tsm_provider = self.tsm_provider or CcxtTickerMarketData("binanceusdm")
            usdttwd_provider = self.usdttwd_provider or CcxtTickerMarketData("bitopro")
            builder = WarmupBuilder(
                live_config=self.config.live,
                qff_intraday_provider=qff_provider,
                qff_fallback_provider=fallback,
                tsm_provider=tsm_provider,
                usdttwd_provider=usdttwd_provider,
            )
            bars = builder.build(
                qff_symbol=contract.symbol,
                qff_expiry=contract.expiry,
                contract_policy_state=contract.policy_state,
                end=end,
            )
            if len(bars) < self.config.strategy.zscore_window:
                raise RuntimeError(
                    f"Warmup produced {len(bars)} bars, "
                    f"need {self.config.strategy.zscore_window}"
                )
            store.replace_warmup_bars(bars)
            store.record_event(
                bars[-1].row_index,
                bars[-1].timestamp,
                "warmup_live",
                "warmup bars written",
                {
                    "bars": len(bars),
                    "qff_symbol": contract.symbol,
                    "qff_expiry": contract.expiry,
                    "contract_policy_state": contract.policy_state,
                },
            )
            store.commit()
            return WarmupResult(
                bars_written=len(bars),
                qff_symbol=contract.symbol,
                start=bars[0].timestamp if bars else None,
                end=bars[-1].timestamp if bars else None,
            )
        finally:
            store.close()


class QffWarmupCheckRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        qff_provider: FubonQffMarketData | None = None,
        taifex_provider: QffWarmupProvider | None = None,
    ) -> None:
        self.config = config
        self.qff_provider = qff_provider
        self.taifex_provider = taifex_provider

    def run(
        self,
        *,
        output_csv: str | None = None,
        end: datetime | None = None,
    ) -> QffWarmupCheckResult:
        if self.config.safety.allow_live_order:
            raise RuntimeError("Refusing qff-warmup-check with allow_live_order=true")

        qff_provider = self.qff_provider or FubonQffMarketData(
            self.config.live.fubon_env_path
        )
        try:
            contract = resolve_qff_contract(self.config, qff_provider)
            end_minute = floor_minute(end or datetime.now().astimezone()) - timedelta(
                minutes=1
            )
            start_minute = end_minute - timedelta(
                minutes=self.config.live.warmup_minutes - 1
            )
            qff_fetch_start = start_minute - QFF_FORWARD_FILL_LOOKBACK
            taifex_provider = self.taifex_provider or TaifexQffTradeDownloader(
                self.config.live.taifex_cache_dir
            )
            taifex_frame = taifex_provider.fetch_1m(
                contract.symbol, qff_fetch_start, end_minute
            )
            fubon_frame = qff_provider.fetch_1m(
                contract.symbol, qff_fetch_start, end_minute
            )
            if taifex_frame.empty:
                raise RuntimeError("TAIFEX QFF warmup data is empty")
            if fubon_frame.empty:
                raise RuntimeError("Fubon QFF intraday candles are empty")

            report = build_qff_warmup_source_report(
                [("taifex", taifex_frame), ("fubon", fubon_frame)],
                start_minute=start_minute,
                end_minute=end_minute,
                qff_fetch_start=qff_fetch_start,
            )
            if len(report.frame) != self.config.live.warmup_minutes:
                raise RuntimeError(
                    f"QFF warmup report has {len(report.frame)} rows, "
                    f"need {self.config.live.warmup_minutes}"
                )
            if report.null_count:
                raise RuntimeError(
                    f"QFF warmup report has {report.null_count} null filled closes"
                )

            resolved_output = self._resolve_output_csv(output_csv)
            if resolved_output is not None:
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                report.frame.to_csv(resolved_output, index=False)

            return QffWarmupCheckResult(
                qff_symbol=contract.symbol,
                qff_expiry=contract.expiry,
                contract_policy_state=contract.policy_state,
                start=start_minute,
                end=end_minute,
                qff_fetch_start=qff_fetch_start,
                report=report,
                output_csv=str(resolved_output) if resolved_output is not None else None,
            )
        finally:
            if self.qff_provider is None:
                qff_provider.close()

    def _resolve_output_csv(self, output_csv: str | None) -> Path | None:
        if output_csv == "":
            return None
        if output_csv is not None:
            return Path(output_csv)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.config.store_path.parent / f"qff_warmup_check_{timestamp}.csv"


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
    tsm = tsm_provider or CcxtTickerMarketData("binanceusdm")
    reporter.event(started_at, "startup", "init_bitopro")
    usdttwd = usdttwd_provider or CcxtTickerMarketData("bitopro")
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


@dataclass
class LiveRuntimeStats:
    iterations: int = 0
    bars_processed: int = 0
    skipped_minutes: int = 0
    plans_recorded: int = 0


@dataclass(frozen=True)
class LiveRuntimeResult:
    iterations: int
    bars_processed: int
    skipped_minutes: int
    plans_recorded: int
    qff_symbol: str


@dataclass(frozen=True)
class LiveModeBarResult:
    result: Any
    plans_recorded: int = 0


class LiveModeHandler:
    mode: str = ""
    auto_warmup_context: str = ""
    complete_contract_switch_after_flat: bool = False

    def validate_config(self, config: AppConfig) -> None:
        raise NotImplementedError

    def on_runtime_ready(
        self,
        store: SQLiteStore,
        *,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> None:
        return None

    def close(self) -> None:
        return None

    def finish_payload(
        self,
        stats: LiveRuntimeStats,
        *,
        resume: bool,
        skip_warmup: bool,
    ) -> dict[str, Any]:
        return {
            "iterations": stats.iterations,
            "bars_processed": stats.bars_processed,
            "skipped_minutes": stats.skipped_minutes,
        }

    def handle_bar(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        reporter: Any,
        strategy: PairStrategy,
        bar: MarketBar,
        decision_snapshot: IndicatorSnapshot,
        decision_spread_type: str | None,
        quote_set: LiveQuoteSet | None,
        force_exit: bool,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        raise NotImplementedError


class PaperLiveModeHandler(LiveModeHandler):
    mode = "live-paper"
    auto_warmup_context = "before_live"
    complete_contract_switch_after_flat = True

    def validate_config(self, config: AppConfig) -> None:
        if config.safety.allow_live_order:
            raise RuntimeError("Refusing live-paper with allow_live_order=true")

    def handle_bar(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        reporter: Any,
        strategy: PairStrategy,
        bar: MarketBar,
        decision_snapshot: IndicatorSnapshot,
        decision_spread_type: str | None,
        quote_set: LiveQuoteSet | None,
        force_exit: bool,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        if force_exit:
            result = strategy.force_exit(
                bar,
                decision_snapshot,
                exit_reason="rollover_force_exit",
            )
            reporter.event(bar.timestamp, "force_exit", "expiry_buffer")
            store.record_event(
                bar.row_index,
                bar.timestamp,
                "rollover_force_exit",
                "forced exit before QFF expiry",
                {"qff_symbol": qff_symbol, "qff_expiry": qff_expiry},
            )
            if result is None:
                result = strategy.on_bar(bar, decision_snapshot)
        else:
            result = strategy.on_bar(bar, decision_snapshot)
            if (
                result.action == StrategyAction.ENTRY_SIGNAL
                and strategy.state.state == StrategyState.ENTRY_PENDING
            ):
                record_live_signal_event(store, reporter, bar, strategy, result)
                result = strategy.fill_pending_entry(bar, decision_snapshot)
            elif (
                result.action == StrategyAction.EXIT_SIGNAL
                and strategy.state.state == StrategyState.EXIT_PENDING
            ):
                record_live_signal_event(store, reporter, bar, strategy, result)
                result = strategy.fill_pending_exit(
                    bar,
                    decision_snapshot,
                    exit_reason="zscore_exit",
                )

        for order in result.orders:
            store.record_order(order)
        for fill in result.fills:
            store.record_fill(fill)
        if result.trade is not None:
            store.record_trade(result.trade)
        if result.action.value != "none":
            store.record_event(
                bar.row_index,
                bar.timestamp,
                result.action.value,
                result.reason,
                {"state": strategy.state.state.value},
            )
        return LiveModeBarResult(result=result)


class DryRunLiveModeHandler(LiveModeHandler):
    mode = "live-dry-run"
    auto_warmup_context = "before_dry_run"

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.recorder: DryRunExecutionRecorder | None = None
        self.coordinator: ExecutionCoordinator | None = None

    def validate_config(self, config: AppConfig) -> None:
        if config.safety.allow_live_order:
            raise RuntimeError("Refusing live-dry-run with allow_live_order=true")

    def on_runtime_ready(
        self,
        store: SQLiteStore,
        *,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> None:
        self.recorder = DryRunExecutionRecorder(
            store,
            allow_live_order=self.config.safety.allow_live_order,
        )
        self.coordinator = ExecutionCoordinator(
            store,
            self.recorder,
            SimulatedExecutionAdapter(),
        )

    def finish_payload(
        self,
        stats: LiveRuntimeStats,
        *,
        resume: bool,
        skip_warmup: bool,
    ) -> dict[str, Any]:
        payload = super().finish_payload(
            stats,
            resume=resume,
            skip_warmup=skip_warmup,
        )
        payload["plans_recorded"] = stats.plans_recorded
        return payload

    def handle_bar(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        reporter: Any,
        strategy: PairStrategy,
        bar: MarketBar,
        decision_snapshot: IndicatorSnapshot,
        decision_spread_type: str | None,
        quote_set: LiveQuoteSet | None,
        force_exit: bool,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        if self.coordinator is None:
            raise RuntimeError("dry-run coordinator is not initialized")

        plan = None
        outcome = None
        if force_exit:
            result, plan, outcome = execute_dry_run_exit(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
                plan_reason="rollover_force_exit",
                exit_reason="rollover_force_exit",
            )
            reporter.event(bar.timestamp, "force_exit", "expiry_buffer")
            store.record_event(
                bar.row_index,
                bar.timestamp,
                "rollover_force_exit",
                "dry-run forced exit intent before QFF expiry",
                {"qff_symbol": qff_symbol, "qff_expiry": qff_expiry},
            )
        elif strategy.state.state == StrategyState.ENTRY_PENDING:
            result, plan, outcome = execute_dry_run_entry(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
            )
        elif strategy.state.state == StrategyState.EXIT_PENDING:
            result, plan, outcome = execute_dry_run_exit(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
                plan_reason="dry_run_exit_intent",
                exit_reason="zscore_exit",
            )
        else:
            result = strategy.on_bar(bar, decision_snapshot)
            if (
                result.action == StrategyAction.ENTRY_SIGNAL
                and strategy.state.state == StrategyState.ENTRY_PENDING
            ):
                record_live_signal_event(store, reporter, bar, strategy, result)
                result, plan, outcome = execute_dry_run_entry(
                    strategy,
                    self.coordinator,
                    bar,
                    decision_snapshot,
                    decision_spread_type,
                    quote_set,
                    self.config.live_execution.max_plan_age_seconds,
                )
            elif (
                result.action == StrategyAction.EXIT_SIGNAL
                and strategy.state.state == StrategyState.EXIT_PENDING
            ):
                record_live_signal_event(store, reporter, bar, strategy, result)
                result, plan, outcome = execute_dry_run_exit(
                    strategy,
                    self.coordinator,
                    bar,
                    decision_snapshot,
                    decision_spread_type,
                    quote_set,
                    self.config.live_execution.max_plan_age_seconds,
                    plan_reason="dry_run_exit_intent",
                    exit_reason="zscore_exit",
                )

        plans_recorded = 0
        if plan is not None:
            plans_recorded = 1
            if outcome is None:
                raise RuntimeError("dry-run execution outcome is missing")
            event_type = dry_run_execution_event_type(outcome)
            reporter.event(
                bar.timestamp,
                "dry_run",
                event_type.replace("dry_run_", ""),
            )
            store.record_event(
                bar.row_index,
                bar.timestamp,
                event_type,
                result.reason,
                {
                    "plan_id": plan.plan_id,
                    "status": plan.status.value,
                    "outcome_status": outcome.status.value,
                    "failed_checks": sum(
                        1 for check in plan.checks if not check.passed
                    ),
                },
            )
            for order in result.orders:
                store.record_order(order)
            for fill in result.fills:
                store.record_fill(fill)
            if result.trade is not None:
                store.record_trade(result.trade)
        elif result.action.value != "none":
            store.record_event(
                bar.row_index,
                bar.timestamp,
                result.action.value,
                result.reason,
                {"state": strategy.state.state.value},
            )
        return LiveModeBarResult(result=result, plans_recorded=plans_recorded)


def record_live_signal_event(
    store: SQLiteStore,
    reporter: Any,
    bar: MarketBar,
    strategy: PairStrategy,
    result: Any,
) -> None:
    store.record_event(
        bar.row_index,
        bar.timestamp,
        result.action.value,
        result.reason,
        {"state": strategy.state.state.value, "same_bar_execution": True},
    )
    reporter.event(
        bar.timestamp,
        result.action.value,
        compact_reason(result.reason),
    )


class LiveExecuteModeHandler(LiveModeHandler):
    mode = "live-execute"
    auto_warmup_context = "before_live_execute"
    complete_contract_switch_after_flat = True

    def __init__(
        self,
        config: AppConfig,
        *,
        binance_adapter: Any | None = None,
        fubon_adapter: Any | None = None,
    ) -> None:
        self.config = config
        self.binance_adapter = binance_adapter
        self.fubon_adapter = fubon_adapter
        self.coordinator: RealExecutionCoordinator | None = None

    def validate_config(self, config: AppConfig) -> None:
        report = evaluate_live_execution_gate(
            config,
            include_reconciliation_checks=False,
            include_plan_checks=False,
        )
        assert_live_execution_gate_open(report)

    def on_runtime_ready(
        self,
        store: SQLiteStore,
        *,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> None:
        if self.binance_adapter is None:
            self.binance_adapter = BinanceTsmExecutionAdapter(
                self.config.live.binance_symbol,
                self.config.live.fubon_env_path,
                leverage=self.config.binance_execution.leverage,
                margin_mode=self.config.binance_execution.margin_mode,
                enforce_leverage=self.config.binance_execution.enforce_leverage,
            )
        if self.fubon_adapter is None:
            self.fubon_adapter = FubonFutureExecutionAdapter(
                qff_symbol,
                self.config.live.fubon_env_path,
            )
        self.coordinator = RealExecutionCoordinator(
            store=store,
            binance_adapter=self.binance_adapter,
            fubon_adapter=self.fubon_adapter,
            qff_first=self.config.live_execution.qff_first,
        )

    def close(self) -> None:
        for adapter in (self.binance_adapter, self.fubon_adapter):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def handle_bar(
        self,
        *,
        config: AppConfig,
        store: SQLiteStore,
        reporter: Any,
        strategy: PairStrategy,
        bar: MarketBar,
        decision_snapshot: IndicatorSnapshot,
        decision_spread_type: str | None,
        quote_set: LiveQuoteSet | None,
        force_exit: bool,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        if self.coordinator is None:
            raise RuntimeError("live execution coordinator is not initialized")

        plan = None
        outcome = None
        if force_exit:
            result, plan, outcome = execute_live_exit(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
                plan_reason="rollover_force_exit",
                exit_reason="rollover_force_exit",
            )
            reporter.event(bar.timestamp, "force_exit", "expiry_buffer")
            store.record_event(
                bar.row_index,
                bar.timestamp,
                "rollover_force_exit",
                "live-execute forced exit before QFF expiry",
                {"qff_symbol": qff_symbol, "qff_expiry": qff_expiry},
            )
        elif strategy.state.state == StrategyState.ENTRY_PENDING:
            result, plan, outcome = execute_live_entry(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
            )
        elif strategy.state.state == StrategyState.EXIT_PENDING:
            result, plan, outcome = execute_live_exit(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
                plan_reason="live_exit_order",
                exit_reason="zscore_exit",
            )
        else:
            result = strategy.on_bar(bar, decision_snapshot)
            if (
                result.action == StrategyAction.ENTRY_SIGNAL
                and strategy.state.state == StrategyState.ENTRY_PENDING
            ):
                record_live_signal_event(store, reporter, bar, strategy, result)
                result, plan, outcome = execute_live_entry(
                    strategy,
                    self.coordinator,
                    bar,
                    decision_snapshot,
                    decision_spread_type,
                    quote_set,
                    self.config.live_execution.max_plan_age_seconds,
                )
            elif (
                result.action == StrategyAction.EXIT_SIGNAL
                and strategy.state.state == StrategyState.EXIT_PENDING
            ):
                record_live_signal_event(store, reporter, bar, strategy, result)
                result, plan, outcome = execute_live_exit(
                    strategy,
                    self.coordinator,
                    bar,
                    decision_snapshot,
                    decision_spread_type,
                    quote_set,
                    self.config.live_execution.max_plan_age_seconds,
                    plan_reason="live_exit_order",
                    exit_reason="zscore_exit",
                )

        plans_recorded = 0
        if plan is not None:
            plans_recorded = 1
            if outcome is None:
                raise RuntimeError("live execution outcome is missing")
            report_live_execution_events(reporter, bar.timestamp, outcome)
            event_type = live_execution_event_type(outcome)
            reporter.event(
                bar.timestamp,
                "live_execution",
                event_type.replace("live_execution_", ""),
            )
            store.record_event(
                bar.row_index,
                bar.timestamp,
                event_type,
                result.reason,
                {
                    "plan_id": plan.plan_id,
                    "status": plan.status.value,
                    "outcome_status": outcome.status.value,
                    "failed_checks": sum(
                        1 for check in plan.checks if not check.passed
                    ),
                },
            )
            orders = result.orders if result.orders else list(outcome.orders)
            fills = result.fills if result.fills else list(outcome.fills)
            for order in orders:
                store.record_order(order)
            for fill in fills:
                store.record_fill(fill)
            if result.trade is not None:
                store.record_trade(result.trade)
        elif result.action.value != "none":
            store.record_event(
                bar.row_index,
                bar.timestamp,
                result.action.value,
                result.reason,
                {"state": strategy.state.state.value},
            )
        return LiveModeBarResult(result=result, plans_recorded=plans_recorded)


class LiveRuntime:
    def __init__(
        self,
        config: AppConfig,
        *,
        handler: LiveModeHandler,
        qff_provider: QuoteProvider | FubonQffMarketData | None = None,
        tsm_provider: QuoteProvider | None = None,
        usdttwd_provider: QuoteProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        reporter: Any | None = None,
    ) -> None:
        self.config = config
        self.handler = handler
        self.qff_provider = qff_provider
        self.tsm_provider = tsm_provider
        self.usdttwd_provider = usdttwd_provider
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleeper = sleeper or time.sleep
        self.reporter = reporter or NullLiveReporter()

    def run(
        self,
        *,
        resume: bool = False,
        reset_store: bool = False,
        max_iterations: int | None = None,
        skip_warmup: bool = False,
    ) -> LiveRuntimeResult:
        self.handler.validate_config(self.config)
        store = SQLiteStore(self.config.store_path)
        live_run_id: int | None = None
        qff_provider_to_close: Any | None = None
        qff_symbol = ""
        stats = LiveRuntimeStats()
        try:
            if reset_store:
                store.reset()
            store.initialize()
            if not resume and not reset_store and store.has_bars():
                raise RuntimeError(
                    "Store already has live bars. Use --resume or --reset-store."
                )

            started_at = ensure_taipei(self.clock())
            runtime = prepare_live_runtime(
                config=self.config,
                store=store,
                resume=resume,
                skip_warmup=skip_warmup,
                qff_provider=self.qff_provider,
                tsm_provider=self.tsm_provider,
                usdttwd_provider=self.usdttwd_provider,
                reporter=self.reporter,
                started_at=started_at,
                auto_warmup_context=self.handler.auto_warmup_context,
            )
            qff_provider = runtime.qff_provider
            tsm_provider = runtime.tsm_provider
            usdttwd_provider = runtime.usdttwd_provider
            qff_provider_to_close = runtime.qff_provider_to_close
            qff_symbol = runtime.qff_symbol
            qff_expiry = runtime.qff_expiry
            strategy = runtime.strategy
            indicator = runtime.indicator
            seed_bars = runtime.seed_bars
            builder = runtime.builder
            next_row_index = runtime.next_row_index
            self.handler.on_runtime_ready(
                store,
                qff_symbol=qff_symbol,
                qff_expiry=qff_expiry,
            )

            live_run_id = store.start_live_run(
                started_at=runtime.started_at,
                mode=self.handler.mode,
                qff_symbol=qff_symbol,
                payload={"resume": resume, "skip_warmup": skip_warmup},
            )
            store.commit()
            self.reporter.event(runtime.started_at, "startup", "live_loop")
            last_non_trading_event_minute: datetime | None = None

            while max_iterations is None or stats.iterations < max_iterations:
                observed_at = ensure_taipei(self.clock())
                session_status = live_session_status(
                    observed_at,
                    self.config.trading_calendar.closed_dates,
                )
                if not session_status.is_trading:
                    builder.reset_current_minute()
                    self.reporter.live_non_trading(
                        observed_at,
                        session_status.next_open_at,
                        session_status.reason,
                    )
                    event_minute = floor_minute(observed_at)
                    if last_non_trading_event_minute != event_minute:
                        store.record_event(
                            next_row_index,
                            event_minute,
                            "non_trading_session",
                            "live session closed",
                            {
                                "reason": session_status.reason,
                                "next_open_at": session_status.next_open_at.isoformat(),
                                "countdown_seconds": int(
                                    session_status.countdown.total_seconds()
                                ),
                            },
                        )
                        store.commit()
                        last_non_trading_event_minute = event_minute
                    stats.iterations += 1
                    self._sleep_if_needed(stats.iterations, max_iterations)
                    continue

                quote_set = LiveQuoteSet(
                    qff=qff_provider.fetch_quote(qff_symbol),
                    tsm=tsm_provider.fetch_quote(self.config.live.binance_symbol),
                    usdttwd=usdttwd_provider.fetch_quote(
                        self.config.live.bitopro_symbol
                    ),
                )
                live_spread_snapshot = estimate_tradable_spreads(
                    quote_set,
                    observed_at,
                    indicator,
                    stale_seconds=self.config.live.stale_seconds,
                    last_qff_close=builder.last_qff_close,
                )
                self.reporter.live(
                    observed_at,
                    live_spread_snapshot,
                    strategy.state.state,
                )
                for quote in (quote_set.qff, quote_set.tsm, quote_set.usdttwd):
                    store.record_market_tick(quote, observed_at)

                build_result = None
                if not should_wait_for_finalize_delay(
                    builder.current_minute,
                    observed_at,
                    self.config.live.minute_finalize_delay_seconds,
                ):
                    build_result = builder.update(quote_set, observed_at)

                if build_result is not None:
                    if build_result.skipped_reason is not None:
                        stats.skipped_minutes += 1
                        self.reporter.warn(
                            observed_at,
                            compact_warning_code(
                                build_result.skipped_reason,
                                build_result.payload,
                            ),
                            "skipped_minute",
                        )
                        store.record_event(
                            next_row_index,
                            floor_minute(observed_at),
                            build_result.skipped_reason,
                            "live minute skipped",
                            build_result.payload,
                        )
                    elif build_result.bar is not None:
                        switch_result = self._process_finalized_bar(
                            store=store,
                            build_result=build_result,
                            qff_provider=qff_provider,
                            tsm_provider=tsm_provider,
                            usdttwd_provider=usdttwd_provider,
                            qff_symbol=qff_symbol,
                            qff_expiry=qff_expiry,
                            strategy=strategy,
                            indicator=indicator,
                            seed_bars=seed_bars,
                            builder=builder,
                            next_row_index=next_row_index,
                            stats=stats,
                            max_iterations=max_iterations,
                        )
                        qff_symbol = switch_result["qff_symbol"]
                        qff_expiry = switch_result["qff_expiry"]
                        indicator = switch_result["indicator"]
                        seed_bars = switch_result["seed_bars"]
                        builder = switch_result["builder"]
                        next_row_index = switch_result["next_row_index"]
                        if switch_result["continued"]:
                            continue
                    store.commit()

                stats.iterations += 1
                self._sleep_if_needed(stats.iterations, max_iterations)

            if live_run_id is not None:
                store.finish_live_run(
                    live_run_id,
                    finished_at=ensure_taipei(self.clock()),
                    status="stopped",
                    payload=self.handler.finish_payload(
                        stats,
                        resume=resume,
                        skip_warmup=skip_warmup,
                    ),
                )
                store.commit()
                live_run_id = None
            return LiveRuntimeResult(
                iterations=stats.iterations,
                bars_processed=stats.bars_processed,
                skipped_minutes=stats.skipped_minutes,
                plans_recorded=stats.plans_recorded,
                qff_symbol=qff_symbol,
            )
        finally:
            if live_run_id is not None:
                try:
                    store.finish_live_run(
                        live_run_id,
                        finished_at=ensure_taipei(self.clock()),
                        status="closed",
                    )
                    store.commit()
                except Exception:
                    store.rollback()
            store.close()
            self.handler.close()
            close_provider_quietly(qff_provider_to_close)

    def _process_finalized_bar(
        self,
        *,
        store: SQLiteStore,
        build_result: Any,
        qff_provider: QuoteProvider | FubonQffMarketData,
        tsm_provider: QuoteProvider,
        usdttwd_provider: QuoteProvider,
        qff_symbol: str,
        qff_expiry: str | None,
        strategy: PairStrategy,
        indicator: IndicatorEngine,
        seed_bars: list[MarketBar],
        builder: LiveMinuteBarBuilder,
        next_row_index: int,
        stats: LiveRuntimeStats,
        max_iterations: int | None,
    ) -> dict[str, Any]:
        bar = replace(
            build_result.bar,
            row_index=next_row_index,
            qff_symbol=qff_symbol,
            qff_expiry=qff_expiry,
            contract_policy_state=strategy.state.contract_policy_state or "active",
        )
        if store.bar_exists_for_timestamp(bar.timestamp):
            self.reporter.event(
                bar.timestamp,
                "duplicate_minute",
                "already_processed",
            )
            store.record_event(
                next_row_index,
                bar.timestamp,
                "duplicate_live_minute",
                "live minute already processed",
            )
            return {
                "qff_symbol": qff_symbol,
                "qff_expiry": qff_expiry,
                "indicator": indicator,
                "seed_bars": seed_bars,
                "builder": builder,
                "next_row_index": next_row_index,
                "continued": False,
            }

        eligible_contract = resolve_qff_contract(
            self.config,
            qff_provider,
            now=bar.timestamp,
        )
        update_eligible_contract_state(strategy.state, eligible_contract)
        if should_switch_contract_before_processing(strategy.state, eligible_contract):
            qff_symbol, qff_expiry, indicator, seed_bars, builder = (
                self._switch_contract_before_processing(
                    store=store,
                    bar=bar,
                    qff_provider=qff_provider,
                    tsm_provider=tsm_provider,
                    usdttwd_provider=usdttwd_provider,
                    qff_symbol=qff_symbol,
                    strategy=strategy,
                    eligible_contract=eligible_contract,
                )
            )
            store.save_state(bar.row_index, bar.timestamp, strategy.state, indicator)
            store.commit()
            stats.iterations += 1
            self._sleep_if_needed(stats.iterations, max_iterations)
            return {
                "qff_symbol": qff_symbol,
                "qff_expiry": qff_expiry,
                "indicator": indicator,
                "seed_bars": seed_bars,
                "builder": builder,
                "next_row_index": next_row_index,
                "continued": True,
            }

        mark_pending_contract_switch_if_needed(strategy.state, eligible_contract)
        if strategy.state.contract_policy_state != bar.contract_policy_state:
            bar = replace(
                bar,
                contract_policy_state=strategy.state.contract_policy_state,
            )
        snapshot = indicator.update(bar)
        tradable_snapshot = build_tradable_snapshot_for_bar(
            build_result.quote_set,
            bar,
            snapshot,
            indicator,
            self.config,
        )
        (
            decision_snapshot,
            decision_spread_type,
            decision_zscore,
            missing_signal_book,
        ) = build_live_decision_snapshot(
            self.config,
            strategy.state,
            snapshot,
            tradable_snapshot,
        )
        if missing_signal_book:
            self.reporter.warn(bar.timestamp, "missing_book", "skip_signal")

        mode_result = self.handler.handle_bar(
            config=self.config,
            store=store,
            reporter=self.reporter,
            strategy=strategy,
            bar=bar,
            decision_snapshot=decision_snapshot,
            decision_spread_type=decision_spread_type,
            quote_set=build_result.quote_set,
            force_exit=should_force_exit_for_contract_policy(
                self.config,
                strategy.state,
                bar.timestamp,
            ),
            qff_symbol=qff_symbol,
            qff_expiry=qff_expiry,
        )
        stats.plans_recorded += mode_result.plans_recorded
        result = mode_result.result
        store.record_bar(
            bar,
            snapshot,
            strategy.state,
            result.unrealized_pnl,
            result.equity,
            result.running_max_equity,
            result.drawdown_twd,
            result.drawdown_pct,
            tradable_snapshot=tradable_snapshot,
            decision_spread_type=decision_spread_type,
            decision_zscore=decision_zscore,
        )
        self.reporter.bar(
            bar.timestamp,
            tradable_snapshot,
            strategy.state.state,
            result.action,
            result.reason,
            result.unrealized_pnl,
            result.equity,
        )
        if result.action.value != "none":
            self.reporter.event(
                bar.timestamp,
                result.action.value,
                compact_reason(result.reason),
            )
        store.save_state(bar.row_index, bar.timestamp, strategy.state, indicator)
        if self.handler.complete_contract_switch_after_flat:
            qff_symbol, qff_expiry, indicator, seed_bars, builder = (
                self._complete_contract_switch_after_flat(
                    store=store,
                    bar=bar,
                    qff_provider=qff_provider,
                    tsm_provider=tsm_provider,
                    usdttwd_provider=usdttwd_provider,
                    qff_symbol=qff_symbol,
                    qff_expiry=qff_expiry,
                    indicator=indicator,
                    seed_bars=seed_bars,
                    builder=builder,
                    strategy=strategy,
                )
            )
        stats.bars_processed += 1
        return {
            "qff_symbol": qff_symbol,
            "qff_expiry": qff_expiry,
            "indicator": indicator,
            "seed_bars": seed_bars,
            "builder": builder,
            "next_row_index": next_row_index + 1,
            "continued": False,
        }

    def _switch_contract_before_processing(
        self,
        *,
        store: SQLiteStore,
        bar: MarketBar,
        qff_provider: QuoteProvider | FubonQffMarketData,
        tsm_provider: QuoteProvider,
        usdttwd_provider: QuoteProvider,
        qff_symbol: str,
        strategy: PairStrategy,
        eligible_contract: QffContractResolution,
    ) -> tuple[
        str,
        str | None,
        IndicatorEngine,
        list[MarketBar],
        LiveMinuteBarBuilder,
    ]:
        if strategy.state.state == StrategyState.ENTRY_PENDING:
            cancel_entry_pending_for_contract_switch(strategy.state)
            self.reporter.event(bar.timestamp, "entry_cancel", "contract_switch")
            store.record_event(
                bar.row_index,
                bar.timestamp,
                "entry_cancel_contract_switch",
                "pending entry canceled before QFF contract switch",
                {
                    "old_qff_symbol": qff_symbol,
                    "new_qff_symbol": eligible_contract.symbol,
                },
            )
        self.reporter.event(
            bar.timestamp,
            "contract_switch",
            f"{qff_symbol}->{eligible_contract.symbol}",
        )
        store.record_event(
            bar.row_index,
            bar.timestamp,
            "contract_switch_detected",
            "flat strategy switching to eligible QFF contract",
            {
                "old_qff_symbol": qff_symbol,
                "new_qff_symbol": eligible_contract.symbol,
            },
        )
        unsubscribe_qff_books_if_supported(qff_provider, qff_symbol)
        qff_symbol, qff_expiry, indicator, seed_bars = switch_to_contract(
            store,
            self.config,
            strategy.state,
            eligible_contract,
            qff_provider=qff_provider,
            tsm_provider=tsm_provider,
            usdttwd_provider=usdttwd_provider,
            end=bar.timestamp,
        )
        subscribe_qff_books_if_supported(
            qff_provider,
            qff_symbol,
            self.reporter,
            bar.timestamp,
        )
        return (
            qff_symbol,
            qff_expiry,
            indicator,
            seed_bars,
            build_live_minute_builder(self.config, seed_bars),
        )

    def _complete_contract_switch_after_flat(
        self,
        *,
        store: SQLiteStore,
        bar: MarketBar,
        qff_provider: QuoteProvider | FubonQffMarketData,
        tsm_provider: QuoteProvider,
        usdttwd_provider: QuoteProvider,
        qff_symbol: str,
        qff_expiry: str | None,
        indicator: IndicatorEngine,
        seed_bars: list[MarketBar],
        builder: LiveMinuteBarBuilder,
        strategy: PairStrategy,
    ) -> tuple[
        str,
        str | None,
        IndicatorEngine,
        list[MarketBar],
        LiveMinuteBarBuilder,
    ]:
        if not (
            strategy.state.state == StrategyState.FLAT
            and strategy.state.pending_symbol_switch
            and strategy.state.eligible_active_qff_symbol
        ):
            return qff_symbol, qff_expiry, indicator, seed_bars, builder

        completed_contract = QffContractResolution(
            symbol=strategy.state.eligible_active_qff_symbol,
            expiry=strategy.state.eligible_active_qff_expiry,
            policy_state="active",
        )
        unsubscribe_qff_books_if_supported(qff_provider, qff_symbol)
        qff_symbol, qff_expiry, indicator, seed_bars = switch_to_contract(
            store,
            self.config,
            strategy.state,
            completed_contract,
            qff_provider=qff_provider,
            tsm_provider=tsm_provider,
            usdttwd_provider=usdttwd_provider,
            end=bar.timestamp,
        )
        subscribe_qff_books_if_supported(
            qff_provider,
            qff_symbol,
            self.reporter,
            bar.timestamp,
        )
        store.record_event(
            bar.row_index,
            bar.timestamp,
            "contract_switch_completed",
            "QFF contract switched after flat state",
            {"qff_symbol": qff_symbol},
        )
        self.reporter.event(bar.timestamp, "contract_switch_done", qff_symbol)
        store.save_state(bar.row_index, bar.timestamp, strategy.state, indicator)
        return (
            qff_symbol,
            qff_expiry,
            indicator,
            seed_bars,
            build_live_minute_builder(self.config, seed_bars),
        )

    def _sleep_if_needed(
        self,
        iterations: int,
        max_iterations: int | None,
    ) -> None:
        if max_iterations is None or iterations < max_iterations:
            self.sleeper(self.config.live.polling_seconds)


class LivePaperRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        qff_provider: QuoteProvider | FubonQffMarketData | None = None,
        tsm_provider: QuoteProvider | None = None,
        usdttwd_provider: QuoteProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        reporter: Any | None = None,
    ) -> None:
        self.runtime = LiveRuntime(
            config,
            handler=PaperLiveModeHandler(),
            qff_provider=qff_provider,
            tsm_provider=tsm_provider,
            usdttwd_provider=usdttwd_provider,
            clock=clock,
            sleeper=sleeper,
            reporter=reporter,
        )

    def run(
        self,
        *,
        resume: bool = False,
        reset_store: bool = False,
        max_iterations: int | None = None,
        skip_warmup: bool = False,
    ) -> LivePaperResult:
        result = self.runtime.run(
            resume=resume,
            reset_store=reset_store,
            max_iterations=max_iterations,
            skip_warmup=skip_warmup,
        )
        return LivePaperResult(
            iterations=result.iterations,
            bars_processed=result.bars_processed,
            skipped_minutes=result.skipped_minutes,
            qff_symbol=result.qff_symbol,
        )


class LiveDryRunRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        qff_provider: QuoteProvider | FubonQffMarketData | None = None,
        tsm_provider: QuoteProvider | None = None,
        usdttwd_provider: QuoteProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        reporter: Any | None = None,
    ) -> None:
        self.runtime = LiveRuntime(
            config,
            handler=DryRunLiveModeHandler(config),
            qff_provider=qff_provider,
            tsm_provider=tsm_provider,
            usdttwd_provider=usdttwd_provider,
            clock=clock,
            sleeper=sleeper,
            reporter=reporter,
        )

    def run(
        self,
        *,
        resume: bool = False,
        reset_store: bool = False,
        max_iterations: int | None = None,
        skip_warmup: bool = False,
    ) -> LiveDryRunResult:
        result = self.runtime.run(
            resume=resume,
            reset_store=reset_store,
            max_iterations=max_iterations,
            skip_warmup=skip_warmup,
        )
        return LiveDryRunResult(
            iterations=result.iterations,
            bars_processed=result.bars_processed,
            skipped_minutes=result.skipped_minutes,
            plans_recorded=result.plans_recorded,
            qff_symbol=result.qff_symbol,
        )


class LiveExecuteRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        qff_provider: QuoteProvider | FubonQffMarketData | None = None,
        tsm_provider: QuoteProvider | None = None,
        usdttwd_provider: QuoteProvider | None = None,
        binance_adapter: Any | None = None,
        fubon_adapter: Any | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        reporter: Any | None = None,
    ) -> None:
        self.runtime = LiveRuntime(
            config,
            handler=LiveExecuteModeHandler(
                config,
                binance_adapter=binance_adapter,
                fubon_adapter=fubon_adapter,
            ),
            qff_provider=qff_provider,
            tsm_provider=tsm_provider,
            usdttwd_provider=usdttwd_provider,
            clock=clock,
            sleeper=sleeper,
            reporter=reporter,
        )

    def run(
        self,
        *,
        resume: bool = False,
        reset_store: bool = False,
        max_iterations: int | None = None,
        skip_warmup: bool = False,
    ) -> LiveRuntimeResult:
        return self.runtime.run(
            resume=resume,
            reset_store=reset_store,
            max_iterations=max_iterations,
            skip_warmup=skip_warmup,
        )


def execute_dry_run_entry(
    strategy: PairStrategy,
    coordinator: ExecutionCoordinator,
    bar: MarketBar,
    snapshot: IndicatorSnapshot,
    decision_spread_type: str | None,
    quote_set: LiveQuoteSet | None,
    max_plan_age_seconds: int | None,
) -> tuple[Any, PairExecutionPlan | None, ExecutionOutcome | None]:
    state = strategy.state
    if state.candidate_time is None:
        state.state = StrategyState.ERROR
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ERROR,
                reason="entry_pending_without_candidate_time",
                bar=bar,
            ),
            None,
            None,
        )
    if state.candidate_direction is None:
        state.state = StrategyState.ERROR
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ERROR,
                reason="entry_pending_without_direction",
                bar=bar,
            ),
            None,
            None,
        )
    delay = minutes_between(state.candidate_time, bar.timestamp)
    if delay > strategy.strategy.max_entry_delay_minutes:
        clear_entry_candidate(state)
        state.state = StrategyState.FLAT
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ENTRY_CANCEL,
                reason="entry_delay_exceeded",
                bar=bar,
            ),
            None,
            None,
        )

    sizing = size_position_for_direction(
        state.candidate_direction,
        bar.tsm_twd_fair,
        bar.qff_close_filled,
        strategy.strategy,
        strategy.fees,
    )
    if sizing is None:
        clear_entry_candidate(state)
        state.state = StrategyState.FLAT
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ENTRY_CANCEL,
                reason="qff_contracts_rounded_to_zero",
                bar=bar,
            ),
            None,
            None,
        )

    costs = fill_costs(
        tsm_units=sizing.tsm_units,
        tsm_price=bar.tsm_twd_fair,
        qff_contracts=sizing.qff_contracts,
        qff_price=bar.qff_close_filled,
        fees=strategy.fees,
    )
    requests = strategy.build_entry_order_requests(
        bar=bar,
        tsm_units=sizing.tsm_units,
        qff_contracts=sizing.qff_contracts,
        costs=costs,
    )
    plan = pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=state.candidate_direction,
        requests=requests,
        reason="dry_run_entry_intent",
        decision_zscore=snapshot.zscore,
        decision_spread_type=decision_spread_type,
    )
    if quote_set is not None:
        plan = apply_live_touch_market_price_policy(
            plan,
            quote_set,
            max_plan_age_seconds=max_plan_age_seconds,
            plan_age_seconds=0.0,
        )
    plan, outcome = coordinator.execute(plan)
    if outcome.filled:
        result = strategy.apply_entry_execution(
            bar=bar,
            snapshot=snapshot,
            sizing=sizing,
            costs=costs,
            orders=list(outcome.orders),
            fills=list(outcome.fills),
            delay_minutes=delay,
            reason="dry_run_filled",
        )
        return result, plan, outcome

    clear_entry_candidate(state)
    state.state = outcome.recommended_state or StrategyState.PAUSED
    return (
        strategy.mark_to_market_result(
            action=StrategyAction.DRY_RUN_INTENT,
            reason=f"dry_run_entry_{outcome.status.value}",
            bar=bar,
        ),
        plan,
        outcome,
    )


def execute_dry_run_exit(
    strategy: PairStrategy,
    coordinator: ExecutionCoordinator,
    bar: MarketBar,
    snapshot: IndicatorSnapshot,
    decision_spread_type: str | None,
    quote_set: LiveQuoteSet | None,
    max_plan_age_seconds: int | None,
    *,
    plan_reason: str,
    exit_reason: str,
) -> tuple[Any, PairExecutionPlan | None, ExecutionOutcome | None]:
    state = strategy.state
    if state.position_direction is None or state.qff_contracts == 0:
        state.state = StrategyState.ERROR
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ERROR,
                reason="exit_without_open_position",
                bar=bar,
            ),
            None,
            None,
        )
    costs = fill_costs(
        tsm_units=state.tsm_units,
        tsm_price=bar.tsm_twd_fair,
        qff_contracts=state.qff_contracts,
        qff_price=bar.qff_close_filled,
        fees=strategy.fees,
    )
    requests = strategy.build_exit_order_requests(bar=bar, costs=costs)
    plan = pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.EXIT,
        direction=state.position_direction,
        requests=requests,
        reason=plan_reason,
        decision_zscore=snapshot.zscore,
        decision_spread_type=decision_spread_type,
    )
    if quote_set is not None:
        plan = apply_live_touch_market_price_policy(
            plan,
            quote_set,
            max_plan_age_seconds=max_plan_age_seconds,
            plan_age_seconds=0.0,
        )
    plan, outcome = coordinator.execute(plan)
    if outcome.filled:
        result = strategy.apply_exit_execution(
            bar=bar,
            snapshot=snapshot,
            costs=costs,
            orders=list(outcome.orders),
            fills=list(outcome.fills),
            exit_reason=exit_reason,
            reason="dry_run_filled"
            if exit_reason != "rollover_force_exit"
            else "rollover_force_exit",
        )
        return result, plan, outcome

    state.state = outcome.recommended_state or StrategyState.PAUSED
    return (
        strategy.mark_to_market_result(
            action=StrategyAction.DRY_RUN_INTENT,
            reason=f"dry_run_exit_{outcome.status.value}",
            bar=bar,
        ),
        plan,
        outcome,
    )


def execute_live_entry(
    strategy: PairStrategy,
    coordinator: RealExecutionCoordinator,
    bar: MarketBar,
    snapshot: IndicatorSnapshot,
    decision_spread_type: str | None,
    quote_set: LiveQuoteSet | None,
    max_plan_age_seconds: int | None,
) -> tuple[Any, PairExecutionPlan | None, ExecutionOutcome | None]:
    state = strategy.state
    if state.candidate_time is None:
        state.state = StrategyState.ERROR
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ERROR,
                reason="entry_pending_without_candidate_time",
                bar=bar,
            ),
            None,
            None,
        )
    if state.candidate_direction is None:
        state.state = StrategyState.ERROR
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ERROR,
                reason="entry_pending_without_direction",
                bar=bar,
            ),
            None,
            None,
        )
    delay = minutes_between(state.candidate_time, bar.timestamp)
    if delay > strategy.strategy.max_entry_delay_minutes:
        clear_entry_candidate(state)
        state.state = StrategyState.FLAT
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ENTRY_CANCEL,
                reason="entry_delay_exceeded",
                bar=bar,
            ),
            None,
            None,
        )

    sizing = size_position_for_direction(
        state.candidate_direction,
        bar.tsm_twd_fair,
        bar.qff_close_filled,
        strategy.strategy,
        strategy.fees,
    )
    if sizing is None:
        clear_entry_candidate(state)
        state.state = StrategyState.FLAT
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ENTRY_CANCEL,
                reason="qff_contracts_rounded_to_zero",
                bar=bar,
            ),
            None,
            None,
        )

    costs = fill_costs(
        tsm_units=sizing.tsm_units,
        tsm_price=bar.tsm_twd_fair,
        qff_contracts=sizing.qff_contracts,
        qff_price=bar.qff_close_filled,
        fees=strategy.fees,
    )
    requests = strategy.build_entry_order_requests(
        bar=bar,
        tsm_units=sizing.tsm_units,
        qff_contracts=sizing.qff_contracts,
        costs=costs,
    )
    plan = pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=state.candidate_direction,
        requests=requests,
        reason="live_entry_order",
        decision_zscore=snapshot.zscore,
        decision_spread_type=decision_spread_type,
    )
    if quote_set is not None:
        plan = apply_live_touch_market_price_policy(
            plan,
            quote_set,
            max_plan_age_seconds=max_plan_age_seconds,
            plan_age_seconds=0.0,
        )
    plan, outcome = coordinator.execute(plan)
    if outcome.filled:
        result = strategy.apply_entry_execution(
            bar=bar,
            snapshot=snapshot,
            sizing=sizing,
            costs=costs,
            orders=list(outcome.orders),
            fills=list(outcome.fills),
            delay_minutes=delay,
            reason="live_filled",
        )
        return result, plan, outcome

    clear_entry_candidate(state)
    state.state = outcome.recommended_state or StrategyState.PAUSED
    return (
        strategy.mark_to_market_result(
            action=StrategyAction.LIVE_EXECUTION,
            reason=f"live_entry_{outcome.status.value}",
            bar=bar,
        ),
        plan,
        outcome,
    )


def execute_live_exit(
    strategy: PairStrategy,
    coordinator: RealExecutionCoordinator,
    bar: MarketBar,
    snapshot: IndicatorSnapshot,
    decision_spread_type: str | None,
    quote_set: LiveQuoteSet | None,
    max_plan_age_seconds: int | None,
    *,
    plan_reason: str,
    exit_reason: str,
) -> tuple[Any, PairExecutionPlan | None, ExecutionOutcome | None]:
    state = strategy.state
    if state.position_direction is None or state.qff_contracts == 0:
        state.state = StrategyState.ERROR
        return (
            strategy.mark_to_market_result(
                action=StrategyAction.ERROR,
                reason="exit_without_open_position",
                bar=bar,
            ),
            None,
            None,
        )
    costs = fill_costs(
        tsm_units=state.tsm_units,
        tsm_price=bar.tsm_twd_fair,
        qff_contracts=state.qff_contracts,
        qff_price=bar.qff_close_filled,
        fees=strategy.fees,
    )
    requests = strategy.build_exit_order_requests(bar=bar, costs=costs)
    plan = pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.EXIT,
        direction=state.position_direction,
        requests=requests,
        reason=plan_reason,
        decision_zscore=snapshot.zscore,
        decision_spread_type=decision_spread_type,
    )
    if quote_set is not None:
        plan = apply_live_touch_market_price_policy(
            plan,
            quote_set,
            max_plan_age_seconds=max_plan_age_seconds,
            plan_age_seconds=0.0,
        )
    plan, outcome = coordinator.execute(plan)
    if outcome.filled:
        result = strategy.apply_exit_execution(
            bar=bar,
            snapshot=snapshot,
            costs=costs,
            orders=list(outcome.orders),
            fills=list(outcome.fills),
            exit_reason=exit_reason,
            reason="live_filled"
            if exit_reason != "rollover_force_exit"
            else "rollover_force_exit",
        )
        return result, plan, outcome

    state.state = outcome.recommended_state or StrategyState.PAUSED
    return (
        strategy.mark_to_market_result(
            action=StrategyAction.LIVE_EXECUTION,
            reason=f"live_exit_{outcome.status.value}",
            bar=bar,
        ),
        plan,
        outcome,
    )


def dry_run_execution_event_type(outcome: ExecutionOutcome) -> str:
    if outcome.status == ExecutionOutcomeStatus.FILLED:
        return "dry_run_execution_filled"
    if outcome.status == ExecutionOutcomeStatus.REJECTED:
        return "dry_run_execution_rejected"
    return "dry_run_execution_failed"


def live_execution_event_type(outcome: ExecutionOutcome) -> str:
    if outcome.status == ExecutionOutcomeStatus.FILLED:
        return "live_execution_filled"
    if outcome.status == ExecutionOutcomeStatus.REJECTED:
        return "live_execution_rejected"
    return "live_execution_failed"


def report_live_execution_events(
    reporter: Any,
    timestamp: datetime,
    outcome: ExecutionOutcome,
) -> None:
    payload = outcome.payload or {}
    for event in payload.get("events", []):
        event_type = str(event.get("event_type") or "")
        event_payload = event.get("payload") or {}
        if event_type == "critical_manual_intervention_required":
            reporter.error(timestamp, "CRITICAL manual intervention required")
        elif event_type in {
            "exposure_breach",
            "single_leg_exposure",
            "imbalanced_pair_exposure",
            "emergency_close_attempted",
            "emergency_close_filled",
            "emergency_close_failed",
        }:
            detail = str(
                event_payload.get("broker")
                or event_payload.get("failed_broker")
                or event_payload.get("outcome_status")
                or ""
            )
            reporter.event(timestamp, event_type, detail)


def clear_entry_candidate(state: StrategyRuntimeState) -> None:
    state.candidate_direction = None
    state.candidate_idx = -1
    state.candidate_time = None
    state.candidate_zscore = None


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


def unsubscribe_qff_books_if_supported(provider: object, symbol: str) -> None:
    unsubscribe = getattr(provider, "unsubscribe_books", None)
    if callable(unsubscribe):
        unsubscribe(symbol)


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


def build_tradable_snapshot_for_bar(
    quote_set: LiveQuoteSet | None,
    bar: Any,
    snapshot: IndicatorSnapshot,
    indicator: IndicatorEngine,
    config: AppConfig,
) -> TradableSpreadSnapshot:
    if quote_set is None:
        return TradableSpreadSnapshot(
            mid_spread=snapshot.spread,
            mid_zscore=snapshot.zscore,
            short_spread=None,
            short_zscore=None,
            long_spread=None,
            long_zscore=None,
            missing_reason="missing_quote",
        )
    tradable_snapshot = estimate_tradable_spreads(
        quote_set,
        bar.timestamp + timedelta(minutes=1),
        indicator,
        stale_seconds=config.live.stale_seconds,
        last_qff_close=bar.qff_close_filled,
    )
    return replace(
        tradable_snapshot,
        mid_spread=snapshot.spread,
        mid_zscore=snapshot.zscore,
    )


def build_live_decision_snapshot(
    config: AppConfig,
    state: StrategyRuntimeState,
    snapshot: IndicatorSnapshot,
    tradable_snapshot: TradableSpreadSnapshot,
) -> tuple[IndicatorSnapshot, str | None, float | None, bool]:
    if state.state == StrategyState.FLAT and snapshot.entry_allowed:
        candidates: list[tuple[str, float, float | None]] = []
        missing_signal_book = False
        if tradable_snapshot.short_zscore is None:
            missing_signal_book = True
        elif tradable_snapshot.short_zscore > config.strategy.entry_z:
            candidates.append(
                (
                    "shortSpread",
                    tradable_snapshot.short_zscore,
                    tradable_snapshot.short_spread,
                )
            )
        if tradable_snapshot.long_zscore is None:
            missing_signal_book = True
        elif tradable_snapshot.long_zscore < -config.strategy.entry_z:
            candidates.append(
                (
                    "longSpread",
                    tradable_snapshot.long_zscore,
                    tradable_snapshot.long_spread,
                )
            )
        if not candidates:
            return (
                replace(snapshot, zscore=None, zscore_valid=False),
                None,
                None,
                missing_signal_book,
            )
        decision_type, decision_zscore, decision_spread = max(
            candidates,
            key=lambda item: abs(item[1]),
        )
        return (
            replace(
                snapshot,
                spread=decision_spread if decision_spread is not None else snapshot.spread,
                zscore=decision_zscore,
                zscore_valid=True,
            ),
            decision_type,
            decision_zscore,
            False,
        )

    if state.state == StrategyState.OPEN and state.position_direction is not None:
        if state.position_direction == Direction.SHORT_TSM_LONG_QFF:
            decision_type = "longSpread"
            decision_spread = tradable_snapshot.long_spread
            decision_zscore = tradable_snapshot.long_zscore
        else:
            decision_type = "shortSpread"
            decision_spread = tradable_snapshot.short_spread
            decision_zscore = tradable_snapshot.short_zscore
        if decision_zscore is None:
            return (
                replace(snapshot, zscore=None, zscore_valid=False),
                decision_type,
                None,
                True,
            )
        return (
            replace(
                snapshot,
                spread=decision_spread if decision_spread is not None else snapshot.spread,
                zscore=decision_zscore,
                zscore_valid=True,
            ),
            decision_type,
            decision_zscore,
            False,
        )

    return snapshot, "mid", snapshot.zscore, False


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


def load_or_build_live_indicator(
    store: SQLiteStore,
    config: AppConfig,
    *,
    qff_symbol: str,
    qff_expiry: str | None,
    policy_state: str,
    qff_provider: QffWarmupProvider,
    tsm_provider: OhlcvProvider,
    usdttwd_provider: OhlcvProvider,
    end: datetime,
    force_rebuild: bool = False,
    allow_rebuild: bool = True,
    reporter: Any | None = None,
    auto_warmup_context: str | None = None,
) -> tuple[IndicatorEngine, list[Any]]:
    seed_bars = []
    if not force_rebuild:
        seed_bars = store.load_indicator_seed_bars(
            config.strategy.zscore_window,
            qff_symbol=qff_symbol,
        )
    if len(seed_bars) < config.strategy.zscore_window:
        if not allow_rebuild:
            raise RuntimeError(
                "Warmup seed is missing or insufficient for live-paper startup: "
                f"found {len(seed_bars)} bars for {qff_symbol}, "
                f"need {config.strategy.zscore_window}. "
                "Remove --skip-warmup or run warmup-live first."
            )
        if auto_warmup_context is not None:
            if reporter is not None:
                reporter.event(end, "warmup_auto", "start")
            store.record_event(
                -1,
                end,
                "warmup_auto_before_live",
                "live-paper auto warmup started",
                {
                    "qff_symbol": qff_symbol,
                    "qff_expiry": qff_expiry,
                    "contract_policy_state": policy_state,
                    "existing_seed_bars": len(seed_bars),
                    "required_seed_bars": config.strategy.zscore_window,
                    "context": auto_warmup_context,
                },
            )
        fallback: QffWarmupProvider | None
        if config.live.taifex_use_network:
            fallback = TaifexQffTradeDownloader(config.live.taifex_cache_dir)
        elif config.live.taifex_qff_1m_csv is not None:
            fallback = CsvQffWarmupProvider(config.live.taifex_qff_1m_csv)
        else:
            fallback = None
        builder = WarmupBuilder(
            live_config=config.live,
            qff_intraday_provider=qff_provider,
            qff_fallback_provider=fallback,
            tsm_provider=tsm_provider,
            usdttwd_provider=usdttwd_provider,
        )
        seed_bars = builder.build(
            qff_symbol=qff_symbol,
            qff_expiry=qff_expiry,
            contract_policy_state=policy_state,
            end=end,
        )
        if len(seed_bars) < config.strategy.zscore_window:
            raise RuntimeError(
                f"Warmup produced {len(seed_bars)} bars, "
                f"need {config.strategy.zscore_window}"
            )
        store.replace_warmup_bars(seed_bars)
        if auto_warmup_context is not None:
            if reporter is not None:
                reporter.event(end, "warmup_auto", f"done_{len(seed_bars)}")
            store.record_event(
                seed_bars[-1].row_index,
                seed_bars[-1].timestamp,
                "warmup_auto_before_live",
                "live-paper auto warmup bars written",
                {
                    "bars": len(seed_bars),
                    "qff_symbol": qff_symbol,
                    "qff_expiry": qff_expiry,
                    "contract_policy_state": policy_state,
                    "context": auto_warmup_context,
                    "force_rebuild": force_rebuild,
                },
            )

    indicator = IndicatorEngine(window=config.strategy.zscore_window)
    for seed_bar in seed_bars:
        indicator.update(seed_bar)
    return indicator, seed_bars


def should_wait_for_finalize_delay(
    current_minute: datetime | None,
    observed_at: datetime,
    delay_seconds: float,
) -> bool:
    if current_minute is None:
        return False
    observed_at = ensure_taipei(observed_at)
    return (
        floor_minute(observed_at) > current_minute
        and observed_at.second + observed_at.microsecond / 1_000_000 < delay_seconds
    )


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
