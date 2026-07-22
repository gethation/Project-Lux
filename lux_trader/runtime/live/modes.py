from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from lux_trader.integrations.binance.execution import BinanceTsmExecutionAdapter
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
    ExecutedPositionError,
    SimulatedExecutionAdapter,
    position_sizing_from_fills,
)
from lux_trader.execution.recorder import DryRunExecutionRecorder
from lux_trader.execution.price_policy import apply_live_touch_market_price_policy
from lux_trader.integrations.binance.market_data import BinanceMarketData
from lux_trader.integrations.bitopro.market_data import BitoProMarketData
from lux_trader.integrations.fubon.execution_process import FubonFutureExecutionProcess
from lux_trader.integrations.fubon.market_data import FubonQffMarketData
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
from lux_trader.core.models import (
    BrokerName,
    Direction,
    IndicatorSnapshot,
    MarketBar,
    StrategyAction,
    StrategyState,
)
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
from lux_trader.ntfy import notify_execution, notify_operational_error


# Force-exit reasons that are routed through the coordinator exit path (as opposed
# to a normal z-score exit). Kept in one place so the exit fill label stays honest.
FORCE_EXIT_REASONS = ("rollover_force_exit", "weekend_force_exit")


def force_exit_report_detail(reason: str) -> str:
    return "weekend" if reason == "weekend_force_exit" else "expiry_buffer"


def force_exit_event_message(reason: str, *, mode_prefix: str = "") -> str:
    tail = (
        "forced exit before weekend market break"
        if reason == "weekend_force_exit"
        else "forced exit before QFF expiry"
    )
    return f"{mode_prefix}{tail}" if mode_prefix else tail


@dataclass
class LiveRuntimeStats:
    iterations: int = 0
    bars_processed: int = 0
    skipped_minutes: int = 0
    plans_recorded: int = 0


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

    def on_resume(
        self,
        store: SQLiteStore,
        *,
        strategy: PairStrategy,
        indicator: Any,
        row_index: int,
        qff_symbol: str,
        qff_expiry: str | None,
        reporter: Any,
        timestamp: datetime,
    ) -> None:
        # Hook for modes that must verify restored state against the broker after
        # a restart. Default is a no-op; live-execute overrides it.
        return None

    def close(self) -> None:
        return None

    def account_brokers_factory(self) -> Any | None:
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
        force_exit_reason: str | None,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        raise NotImplementedError


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
        force_exit_reason: str | None,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        if self.coordinator is None:
            raise RuntimeError("dry-run coordinator is not initialized")

        plan = None
        outcome = None
        if force_exit_reason is not None:
            result, plan, outcome = execute_dry_run_exit(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
                plan_reason=force_exit_reason,
                exit_reason=force_exit_reason,
            )
            reporter.event(
                bar.timestamp,
                "force_exit",
                force_exit_report_detail(force_exit_reason),
            )
            store.record_event(
                bar.row_index,
                bar.timestamp,
                force_exit_reason,
                force_exit_event_message(force_exit_reason, mode_prefix="dry-run "),
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
            notify_execution(reporter, bar.timestamp, plan, outcome, result)
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
        readonly_brokers: tuple[ReadOnlyBroker, ...] | None = None,
        post_trade_reconciler: PostTradeReconciler | None = None,
    ) -> None:
        self.config = config
        self.binance_adapter = binance_adapter
        self.fubon_adapter = fubon_adapter
        self.readonly_brokers = readonly_brokers
        self.post_trade_reconciler = post_trade_reconciler
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
            self.fubon_adapter = FubonFutureExecutionProcess(
                qff_symbol,
                self.config.live.fubon_env_path,
            )
        if self.readonly_brokers is None:
            self.readonly_brokers = (
                self.fubon_adapter,
                BinanceReadOnlyBroker(
                    self.config.live.binance_symbol,
                    self.config.live.fubon_env_path,
                ),
            )
        if self.post_trade_reconciler is None:
            self.post_trade_reconciler = PostTradeReconciler(
                tsm_units_tolerance=(
                    self.config.broker_reconciliation.tsm_units_tolerance
                ),
                qff_contract_tolerance=(
                    self.config.broker_reconciliation.qff_contract_tolerance
                ),
            )
        self.coordinator = RealExecutionCoordinator(
            store=store,
            binance_adapter=self.binance_adapter,
            fubon_adapter=self.fubon_adapter,
            qff_first=self.config.live_execution.qff_first,
        )

    def account_brokers_factory(self) -> Any | None:
        if self.readonly_brokers is None:
            return None
        return lambda: self.readonly_brokers

    def on_resume(
        self,
        store: SQLiteStore,
        *,
        strategy: PairStrategy,
        indicator: Any,
        row_index: int,
        qff_symbol: str,
        qff_expiry: str | None,
        reporter: Any,
        timestamp: datetime,
    ) -> None:
        # After a restart, a restored open position must still exist at the broker.
        # Run the same read-only reconciliation used post-trade; pause on mismatch
        # so an unattended loop never manages a phantom position (e.g. one that was
        # liquidated or manually closed while the process was down).
        state = strategy.state
        has_position = (
            state.position_direction is not None
            or abs(float(state.tsm_units or 0.0)) > 1e-12
            or int(state.qff_contracts or 0) != 0
        )
        if not has_position:
            return
        if self.post_trade_reconciler is None or self.readonly_brokers is None:
            raise RuntimeError("post-trade reconciliation is not initialized")
        report = self.post_trade_reconciler.reconcile(
            store=store,
            strategy_state=state,
            brokers=self.readonly_brokers,
            tsm_symbol=self.config.live.binance_symbol,
            qff_symbol=state.trading_qff_symbol or qff_symbol,
            timestamp=timestamp,
        )
        run_id = store.record_reconciliation_report(report)
        matched = report.status == ReconciliationStatus.MATCHED
        store.record_event(
            row_index,
            timestamp,
            "resume_reconciliation_matched"
            if matched
            else "resume_reconciliation_mismatch",
            f"resume reconciliation status={report.status.value}",
            {
                "run_id": run_id,
                "status": report.status.value,
                "issue_count": len(report.issues),
            },
        )
        if matched:
            reporter.event(timestamp, "resume_reconciliation", "matched")
            return
        state.state = StrategyState.PAUSED
        store.save_state(row_index, timestamp, state, indicator)
        reporter.warn(timestamp, "resume_reconciliation", report.status.value)
        reporter.event(timestamp, "resume_reconciliation", "paused")
        notify_operational_error(
            reporter,
            timestamp,
            "resume_reconciliation_mismatch",
            report.status.value,
        )

    def close(self) -> None:
        for adapter in (self.binance_adapter, self.fubon_adapter):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        for broker in self.readonly_brokers or ():
            close = getattr(broker, "close", None)
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
        force_exit_reason: str | None,
        qff_symbol: str,
        qff_expiry: str | None,
    ) -> LiveModeBarResult:
        if self.coordinator is None:
            raise RuntimeError("live execution coordinator is not initialized")

        plan = None
        outcome = None
        if force_exit_reason is not None:
            result, plan, outcome = execute_live_exit(
                strategy,
                self.coordinator,
                bar,
                decision_snapshot,
                decision_spread_type,
                quote_set,
                self.config.live_execution.max_plan_age_seconds,
                plan_reason=force_exit_reason,
                exit_reason=force_exit_reason,
            )
            reporter.event(
                bar.timestamp,
                "force_exit",
                force_exit_report_detail(force_exit_reason),
            )
            store.record_event(
                bar.row_index,
                bar.timestamp,
                force_exit_reason,
                force_exit_event_message(force_exit_reason, mode_prefix="live-execute "),
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
            notify_execution(reporter, bar.timestamp, plan, outcome, result)
            if result.reason == "live_entry_fill_mismatch":
                notify_operational_error(
                    reporter,
                    bar.timestamp,
                    "live_entry_fill_mismatch",
                    "filled legs could not form a valid pair position",
                )
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
            post_report = self._run_post_trade_reconciliation(
                store=store,
                reporter=reporter,
                strategy=strategy,
                bar=bar,
                qff_symbol=qff_symbol,
            )
            if post_report.status != ReconciliationStatus.MATCHED:
                strategy.state.state = StrategyState.PAUSED
                result.action = StrategyAction.LIVE_EXECUTION
                result.reason = "post_trade_reconciliation_mismatch"
        elif result.action.value != "none":
            store.record_event(
                bar.row_index,
                bar.timestamp,
                result.action.value,
                result.reason,
                {"state": strategy.state.state.value},
            )
        return LiveModeBarResult(result=result, plans_recorded=plans_recorded)

    def _run_post_trade_reconciliation(
        self,
        *,
        store: SQLiteStore,
        reporter: Any,
        strategy: PairStrategy,
        bar: MarketBar,
        qff_symbol: str,
    ) -> ReconciliationReport:
        if self.post_trade_reconciler is None or self.readonly_brokers is None:
            raise RuntimeError("post-trade reconciliation is not initialized")
        report = self.post_trade_reconciler.reconcile(
            store=store,
            strategy_state=strategy.state,
            brokers=self.readonly_brokers,
            tsm_symbol=self.config.live.binance_symbol,
            qff_symbol=strategy.state.trading_qff_symbol or qff_symbol,
            timestamp=bar.timestamp,
        )
        run_id = store.record_reconciliation_report(report)
        report_payload = report.to_jsonable()
        event_type = (
            "post_trade_reconciliation_matched"
            if report.status == ReconciliationStatus.MATCHED
            else "post_trade_reconciliation_mismatch"
        )
        store.record_event(
            bar.row_index,
            bar.timestamp,
            event_type,
            f"post-trade reconciliation status={report.status.value}",
            {
                "run_id": run_id,
                "status": report.status.value,
                "issue_count": len(report.issues),
                "issues": report_payload["issues"],
            },
        )
        if report.status == ReconciliationStatus.MATCHED:
            reporter.event(bar.timestamp, "post_trade_reconciliation", "matched")
        else:
            reporter.warn(
                bar.timestamp,
                "post_trade_reconciliation",
                report.status.value,
            )
            reporter.event(
                bar.timestamp,
                "post_trade_reconciliation",
                "paused",
            )
            notify_operational_error(
                reporter,
                bar.timestamp,
                "post_trade_reconciliation_mismatch",
                report.status.value,
            )
        return report


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
            tsm_contract_multiplier=strategy.fees.tsm_contract_multiplier,
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
            tsm_contract_multiplier=strategy.fees.tsm_contract_multiplier,
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
            reason=exit_reason
            if exit_reason in FORCE_EXIT_REASONS
            else "dry_run_filled",
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
            tsm_contract_multiplier=strategy.fees.tsm_contract_multiplier,
        )
    plan, outcome = coordinator.execute(plan)
    if outcome.filled:
        try:
            executed_sizing = position_sizing_from_fills(
                state.candidate_direction,
                outcome.fills,
                tsm_symbol=strategy.tsm_symbol,
                qff_symbol=bar.qff_symbol or "QFF",
                qff_contract_multiplier=strategy.fees.qff_contract_multiplier,
            )
        except ExecutedPositionError:
            clear_entry_candidate(state)
            state.state = StrategyState.PAUSED
            return (
                strategy.mark_to_market_result(
                    action=StrategyAction.LIVE_EXECUTION,
                    reason="live_entry_fill_mismatch",
                    bar=bar,
                ),
                plan,
                outcome,
            )
        executed_costs = fill_costs(
            tsm_units=executed_sizing.tsm_units,
            tsm_price=bar.tsm_twd_fair,
            qff_contracts=executed_sizing.qff_contracts,
            qff_price=bar.qff_close_filled,
            fees=strategy.fees,
        )
        result = strategy.apply_entry_execution(
            bar=bar,
            snapshot=snapshot,
            sizing=executed_sizing,
            costs=executed_costs,
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
            tsm_contract_multiplier=strategy.fees.tsm_contract_multiplier,
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
            reason=exit_reason
            if exit_reason in FORCE_EXIT_REASONS
            else "live_filled",
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
    timing_gap = payload.get("primary_leg_timing_gap")
    if isinstance(timing_gap, dict):
        submit_gap = timing_gap.get("submit_start_gap_seconds")
        handoff_gap = timing_gap.get("submit_handoff_gap_seconds")
        detail_parts = []
        if submit_gap is not None:
            detail_parts.append(f"submit_start={float(submit_gap):.3f}s")
        if handoff_gap is not None:
            detail_parts.append(f"submit_handoff={float(handoff_gap):.3f}s")
        if detail_parts:
            reporter.event(timestamp, "leg_timing_gap", " ".join(detail_parts))
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

