from __future__ import annotations

import subprocess
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
from lux_trader.margin.display import AccountDisplay, AccountDisplayProvider
from lux_trader.margin.monitor import MarginMonitor, READONLY_BROKER_ENV
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

from lux_trader.runtime.live.bootstrap import (
    close_provider_quietly,
    fetch_quote_or_cached,
    prepare_live_runtime,
    run_live_startup_preflight,
)
from lux_trader.runtime.live.contracts import (
    QFF_RECONNECT_GRACE_SECONDS,
    QFF_WATCHDOG_SECONDS,
    QffContractResolution,
    cancel_entry_pending_for_contract_switch,
    mark_pending_contract_switch_if_needed,
    qff_book_age_seconds,
    qff_book_is_fresh_for_signal,
    resolve_qff_contract,
    reconnect_qff_provider_if_supported,
    resolve_force_exit_reason,
    restart_qff_books_if_supported,
    should_switch_contract_before_processing,
    subscribe_qff_books_if_supported,
    switch_to_contract,
    teardown_qff_books_if_supported,
    unsubscribe_qff_books_if_supported,
    update_eligible_contract_state,
)
from lux_trader.runtime.live.modes import (
    DryRunLiveModeHandler,
    LiveExecuteModeHandler,
    LiveModeHandler,
    LiveRuntimeStats,
)
from lux_trader.runtime.live.bootstrap import build_live_minute_builder


@dataclass(frozen=True)
class LiveDryRunResult:
    iterations: int
    bars_processed: int
    skipped_minutes: int
    plans_recorded: int
    qff_symbol: str


@dataclass(frozen=True)
class LiveRuntimeResult:
    iterations: int
    bars_processed: int
    skipped_minutes: int
    plans_recorded: int
    qff_symbol: str


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
        margin_brokers_factory: Callable[[], tuple[Any, Any]] | None = None,
    ) -> None:
        self.config = config
        self.handler = handler
        self.qff_provider = qff_provider
        self.tsm_provider = tsm_provider
        self.usdttwd_provider = usdttwd_provider
        self._uses_default_clock = clock is None
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleeper = sleeper or time.sleep
        self.reporter = reporter or NullLiveReporter()
        self.margin_brokers_factory = margin_brokers_factory

    def run(
        self,
        *,
        resume: bool = False,
        reset_store: bool = False,
        max_iterations: int | None = None,
        skip_warmup: bool = False,
    ) -> LiveRuntimeResult:
        self.handler.validate_config(self.config)
        if resume and skip_warmup:
            raise RuntimeError(
                "--resume requires a fresh warmup rebuild; remove --skip-warmup"
            )
        if self._uses_default_clock:
            run_live_startup_preflight(
                self.config,
                self.reporter,
                self.clock,
            )
        store = SQLiteStore(self.config.store_path)
        live_run_id: int | None = None
        margin_monitor: MarginMonitor | None = None
        account_display: AccountDisplayProvider | None = None
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
            if resume:
                # After a restart, verify any restored open position against the
                # broker before trading again; the handler pauses on mismatch.
                self.handler.on_resume(
                    store,
                    strategy=strategy,
                    indicator=indicator,
                    row_index=max(next_row_index - 1, 0),
                    qff_symbol=qff_symbol,
                    qff_expiry=qff_expiry,
                    reporter=self.reporter,
                    timestamp=runtime.started_at,
                )

            live_run_id = store.start_live_run(
                started_at=runtime.started_at,
                mode=self.handler.mode,
                qff_symbol=qff_symbol,
                payload={"resume": resume, "skip_warmup": skip_warmup},
            )
            store.commit()
            self.reporter.event(runtime.started_at, "startup", "live_loop")
            def _usdttwd_rate() -> float | None:
                return getattr(
                    usdttwd_provider.fetch_quote(self.config.live.bitopro_symbol),
                    "price",
                    None,
                )

            # Live account panel (real pnl / margin water level). Owns the shared
            # read-only broker pair; the margin monitor reuses it so Fubon is not
            # logged in twice.
            # Not given the runtime clock on purpose: refresh() runs every bar and
            # only needs a wall-clock display timestamp; consuming an injected
            # (finite, test-budgeted) clock here would starve the loop.
            account_display = AccountDisplayProvider(
                self.config,
                usdttwd_rate=_usdttwd_rate,
                brokers_factory=self.margin_brokers_factory,
            )
            if not account_display.enabled():
                self.reporter.event(
                    runtime.started_at,
                    "account_panel",
                    f"disabled: set {READONLY_BROKER_ENV}=1 for live pnl/margin",
                )
            margin_monitor = MarginMonitor(
                self.config,
                usdttwd_rate=_usdttwd_rate,
                brokers_factory=account_display.ensure_brokers,
                clock=self.clock,
            )
            last_non_trading_event_minute: datetime | None = None
            qff_books_torn_down_for_non_trading = False
            qff_reconnecting_until: datetime | None = None
            last_qff_books_restart_at: datetime | None = None
            last_qff_reconnect_warning_minute: datetime | None = None
            last_quotes: dict[str, Any] = {}
            last_fetch_warning_minute: dict[str, datetime] = {}

            while max_iterations is None or stats.iterations < max_iterations:
                observed_at = ensure_taipei(self.clock())
                session_status = live_session_status(
                    observed_at,
                    self.config.trading_calendar.closed_dates,
                )
                if not session_status.is_trading:
                    if not qff_books_torn_down_for_non_trading:
                        teardown_qff_books_if_supported(qff_provider)
                        qff_books_torn_down_for_non_trading = True
                        qff_reconnecting_until = None
                        last_qff_books_restart_at = None
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

                # Broker accounting endpoints can be unavailable during the
                # post-session settlement window. Defer due checks until the
                # first trading iteration instead of querying while closed.
                margin_monitor.maybe_run(
                    observed_at,
                    strategy_state=strategy.state,
                    store=store,
                    reporter=self.reporter,
                )

                if qff_books_torn_down_for_non_trading:
                    # Re-login first so the session starts on a fresh marketdata
                    # token, then restart the books on the new session.
                    reconnect_qff_provider_if_supported(
                        qff_provider,
                        self.reporter,
                        observed_at,
                    )
                    last_qff_books_restart_at = restart_qff_books_if_supported(
                        qff_provider,
                        qff_symbol,
                        self.reporter,
                        observed_at,
                        last_restart_at=last_qff_books_restart_at,
                    )
                    qff_reconnecting_until = observed_at + timedelta(
                        seconds=QFF_RECONNECT_GRACE_SECONDS
                    )
                    qff_books_torn_down_for_non_trading = False

                qff_quote = fetch_quote_or_cached(
                    qff_provider,
                    qff_symbol,
                    "qff",
                    last_quotes,
                    self.reporter,
                    observed_at,
                    last_fetch_warning_minute,
                )
                tsm_quote = fetch_quote_or_cached(
                    tsm_provider,
                    self.config.live.binance_symbol,
                    "tsm",
                    last_quotes,
                    self.reporter,
                    observed_at,
                    last_fetch_warning_minute,
                )
                usdttwd_quote = fetch_quote_or_cached(
                    usdttwd_provider,
                    self.config.live.bitopro_symbol,
                    "usdttwd",
                    last_quotes,
                    self.reporter,
                    observed_at,
                    last_fetch_warning_minute,
                )
                if qff_quote is None or tsm_quote is None or usdttwd_quote is None:
                    fetch_key = "quote_set"
                    warning_minute = floor_minute(observed_at)
                    if last_fetch_warning_minute.get(fetch_key) != warning_minute:
                        self.reporter.warn(
                            observed_at,
                            "market_data_fetch",
                            "skip_iteration",
                        )
                        last_fetch_warning_minute[fetch_key] = warning_minute
                    stats.iterations += 1
                    self._sleep_if_needed(stats.iterations, max_iterations)
                    continue
                quote_set = LiveQuoteSet(
                    qff=qff_quote,
                    tsm=tsm_quote,
                    usdttwd=usdttwd_quote,
                )
                qff_reconnecting = (
                    qff_reconnecting_until is not None
                    and observed_at <= qff_reconnecting_until
                    and not qff_book_is_fresh_for_signal(
                        quote_set.qff,
                        observed_at,
                        self.config,
                    )
                )
                if qff_book_is_fresh_for_signal(quote_set.qff, observed_at, self.config):
                    qff_reconnecting_until = None
                    qff_reconnecting = False
                elif qff_book_age_seconds(quote_set.qff, observed_at) > QFF_WATCHDOG_SECONDS:
                    restarted_at = restart_qff_books_if_supported(
                        qff_provider,
                        qff_symbol,
                        self.reporter,
                        observed_at,
                        last_restart_at=last_qff_books_restart_at,
                    )
                    if restarted_at != last_qff_books_restart_at:
                        qff_reconnecting_until = observed_at + timedelta(
                            seconds=QFF_RECONNECT_GRACE_SECONDS
                        )
                        qff_reconnecting = True
                    last_qff_books_restart_at = restarted_at
                live_spread_snapshot = estimate_tradable_spreads(
                    quote_set,
                    observed_at,
                    indicator,
                    stale_seconds=self.config.live.stale_seconds,
                    qff_book_stale_seconds=self.config.live.qff_book_stale_seconds,
                    last_qff_close=builder.last_qff_close,
                )
                if qff_reconnecting and (
                    live_spread_snapshot.short_spread is None
                    or live_spread_snapshot.long_spread is None
                ):
                    live_spread_snapshot = replace(
                        live_spread_snapshot,
                        missing_reason="qff_reconnecting",
                    )
                    warning_minute = floor_minute(observed_at)
                    if last_qff_reconnect_warning_minute != warning_minute:
                        self.reporter.warn(
                            observed_at,
                            "qff_reconnecting",
                            "skip_signal",
                        )
                        last_qff_reconnect_warning_minute = warning_minute
                self.reporter.live(
                    observed_at,
                    live_spread_snapshot,
                    strategy.state,
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
                            account_display=account_display,
                            signal_block_override="qff_reconnecting"
                            if qff_reconnecting
                            else None,
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
            if account_display is not None:
                account_display.close()
            if margin_monitor is not None:
                margin_monitor.close()
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
        account_display: AccountDisplayProvider | None = None,
        signal_block_override: str | None = None,
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

        try:
            eligible_contract = resolve_qff_contract(
                self.config,
                qff_provider,
                now=bar.timestamp,
            )
        except Exception as exc:
            # A transient market-data failure (e.g. token refresh in flight) must
            # not crash the loop. Keep the current contract and retry next minute;
            # the session-entry re-login normally restores the marketdata token.
            eligible_contract = None
            self.reporter.warn(
                bar.timestamp,
                "qff_contract",
                f"resolve_failed:{type(exc).__name__}",
            )
        if eligible_contract is not None:
            update_eligible_contract_state(strategy.state, eligible_contract)
            if should_switch_contract_before_processing(
                strategy.state, eligible_contract
            ):
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
        if signal_block_override is not None and (
            tradable_snapshot.short_spread is None
            or tradable_snapshot.long_spread is None
        ):
            tradable_snapshot = replace(
                tradable_snapshot,
                missing_reason=signal_block_override,
            )
        (
            decision_snapshot,
            decision_spread_type,
            decision_zscore,
            signal_block_reason,
        ) = build_live_decision_snapshot(
            self.config,
            strategy.state,
            snapshot,
            tradable_snapshot,
        )
        if signal_block_reason is not None:
            self.reporter.warn(bar.timestamp, signal_block_reason, "skip_signal")

        mode_result = self.handler.handle_bar(
            config=self.config,
            store=store,
            reporter=self.reporter,
            strategy=strategy,
            bar=bar,
            decision_snapshot=decision_snapshot,
            decision_spread_type=decision_spread_type,
            quote_set=build_result.quote_set,
            force_exit_reason=resolve_force_exit_reason(
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
        account_snapshot: AccountDisplay | None = None
        if account_display is not None:
            account_snapshot = account_display.refresh(
                notional_twd=self._current_leg_notional_twd(bar, strategy.state)
            )
        self.reporter.bar(
            bar.timestamp,
            tradable_snapshot,
            strategy.state,
            result.action,
            result.reason,
            result.unrealized_pnl,
            result.equity,
            account_display=account_snapshot,
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

    def _current_leg_notional_twd(
        self, bar: MarketBar, state: StrategyRuntimeState
    ) -> float:
        """Current-price single-leg notional for the margin-level denominator.

        Holding -> mark the held Fubon leg to the current price; flat -> price a
        standard leg at the current bar so the 保證金水位 still shows. Falls back
        to the configured leg notional when a price is unavailable.
        """
        fallback = (
            self.config.margin_management.leg_notional_twd
            if self.config.margin_management.leg_notional_twd > 0
            else self.config.strategy.leg_notional_twd
        )
        qff_price = getattr(bar, "qff_close_filled", None)
        tsm_price = getattr(bar, "tsm_twd_fair", None)
        contracts = int(getattr(state, "qff_contracts", 0) or 0)
        if contracts != 0 and qff_price:
            return abs(contracts) * self.config.fees.qff_contract_multiplier * qff_price
        if tsm_price and qff_price:
            sizing = size_position_for_direction(
                Direction.LONG_TSM_SHORT_QFF,
                tsm_price,
                qff_price,
                self.config.strategy,
                self.config.fees,
            )
            if sizing is not None and sizing.actual_leg_notional_twd > 0:
                return sizing.actual_leg_notional_twd
        return fallback

    def _sleep_if_needed(
        self,
        iterations: int,
        max_iterations: int | None,
    ) -> None:
        if max_iterations is None or iterations < max_iterations:
            self.sleeper(self.config.live.polling_seconds)


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
        readonly_brokers: tuple[ReadOnlyBroker, ...] | None = None,
        post_trade_reconciler: PostTradeReconciler | None = None,
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
                readonly_brokers=readonly_brokers,
                post_trade_reconciler=post_trade_reconciler,
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
        qff_book_stale_seconds=config.live.qff_book_stale_seconds,
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
) -> tuple[IndicatorSnapshot, str | None, float | None, str | None]:
    if state.state == StrategyState.FLAT and snapshot.entry_allowed:
        candidates: list[tuple[str, float, float | None]] = []
        signal_block_reason: str | None = None
        if tradable_snapshot.short_zscore is None:
            signal_block_reason = tradable_snapshot.missing_reason or "missing_book"
        elif tradable_snapshot.short_zscore > config.strategy.entry_z:
            candidates.append(
                (
                    "shortSpread",
                    tradable_snapshot.short_zscore,
                    tradable_snapshot.short_spread,
                )
            )
        if tradable_snapshot.long_zscore is None:
            signal_block_reason = tradable_snapshot.missing_reason or "missing_book"
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
                signal_block_reason,
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
            None,
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
                tradable_snapshot.missing_reason or "missing_book",
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
            None,
        )

    return snapshot, "mid", snapshot.zscore, None


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

