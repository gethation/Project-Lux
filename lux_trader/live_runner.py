from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .brokers import PaperBroker
from .config import AppConfig
from .contract_policy import ExpiryBufferContractPolicy, QffContractSelection
from .indicator import IndicatorEngine
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
from .models import Direction, IndicatorSnapshot, StrategyState
from .strategy import PairStrategy, StrategyRuntimeState
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
        self.config = config
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
    ) -> LivePaperResult:
        if self.config.safety.allow_live_order:
            raise RuntimeError("Refusing live-paper with allow_live_order=true")
        store = SQLiteStore(self.config.store_path)
        live_run_id: int | None = None
        qff_provider_to_close: Any | None = None
        try:
            if reset_store:
                store.reset()
            store.initialize()
            if not resume and not reset_store and store.has_bars():
                raise RuntimeError(
                    "Store already has live bars. Use --resume or --reset-store."
                )

            started_at = ensure_taipei(self.clock())
            self.reporter.event(started_at, "startup", "store_ready")
            self.reporter.event(started_at, "startup", "init_fubon")
            qff_provider = self.qff_provider or FubonQffMarketData(
                self.config.live.fubon_env_path
            )
            if self.qff_provider is None:
                qff_provider_to_close = qff_provider
            self.reporter.event(started_at, "startup", "init_binance")
            tsm_provider = self.tsm_provider or CcxtTickerMarketData("binanceusdm")
            self.reporter.event(started_at, "startup", "init_bitopro")
            usdttwd_provider = self.usdttwd_provider or CcxtTickerMarketData("bitopro")
            self.reporter.event(started_at, "startup", "resolve_qff")
            initial_contract = resolve_qff_contract(
                self.config,
                qff_provider,
                now=started_at,
            )
            self.reporter.event(started_at, "startup", f"qff={initial_contract.symbol}")

            resume_state = store.load_resume_state() if resume else None
            strategy_state = (
                resume_state.strategy
                if resume_state is not None
                else StrategyRuntimeState(
                    running_max_equity=self.config.strategy.initial_capital_twd
                )
            )
            initialize_contract_state(strategy_state, initial_contract)
            qff_symbol = strategy_state.trading_qff_symbol or initial_contract.symbol
            qff_expiry = strategy_state.trading_qff_expiry or initial_contract.expiry
            subscribe_qff_books_if_supported(
                qff_provider,
                qff_symbol,
                self.reporter,
                started_at,
            )

            indicator, seed_bars = load_or_build_live_indicator(
                store,
                self.config,
                qff_symbol=qff_symbol,
                qff_expiry=qff_expiry,
                policy_state=strategy_state.contract_policy_state or "active",
                qff_provider=qff_provider,
                tsm_provider=tsm_provider,
                usdttwd_provider=usdttwd_provider,
                end=started_at,
                allow_rebuild=not skip_warmup,
                reporter=self.reporter,
                auto_warmup_context="before_live",
            )
            self.reporter.event(started_at, "startup", f"seed_ready_{len(seed_bars)}")
            strategy = PairStrategy(
                self.config.strategy,
                self.config.fees,
                PaperBroker(),
                state=strategy_state,
                tsm_symbol=self.config.live.binance_symbol,
            )
            builder = LiveMinuteBarBuilder(
                stale_seconds=self.config.live.stale_seconds,
                max_leg_timestamp_skew_seconds=(
                    self.config.live.max_leg_timestamp_skew_seconds
                ),
            )
            builder.last_qff_close = seed_bars[-1].qff_close_filled
            next_row_index = store.latest_bar_row_index() + 1
            iterations = 0
            bars_processed = 0
            skipped_minutes = 0
            live_run_id = store.start_live_run(
                started_at=started_at,
                mode="live-paper",
                qff_symbol=qff_symbol,
                payload={"resume": resume, "skip_warmup": skip_warmup},
            )
            store.commit()
            self.reporter.event(started_at, "startup", "live_loop")

            while max_iterations is None or iterations < max_iterations:
                observed_at = ensure_taipei(self.clock())
                quote_set = LiveQuoteSet(
                    qff=qff_provider.fetch_quote(qff_symbol),
                    tsm=tsm_provider.fetch_quote(self.config.live.binance_symbol),
                    usdttwd=usdttwd_provider.fetch_quote(self.config.live.bitopro_symbol),
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
                        skipped_minutes += 1
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
                        bar = replace(
                            build_result.bar,
                            row_index=next_row_index,
                            qff_symbol=qff_symbol,
                            qff_expiry=qff_expiry,
                            contract_policy_state=(
                                strategy.state.contract_policy_state or "active"
                            ),
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
                        else:
                            eligible_contract = resolve_qff_contract(
                                self.config,
                                qff_provider,
                                now=bar.timestamp,
                            )
                            update_eligible_contract_state(
                                strategy.state,
                                eligible_contract,
                            )
                            if should_switch_contract_before_processing(
                                strategy.state,
                                eligible_contract,
                            ):
                                if strategy.state.state == StrategyState.ENTRY_PENDING:
                                    cancel_entry_pending_for_contract_switch(
                                        strategy.state
                                    )
                                    self.reporter.event(
                                        bar.timestamp,
                                        "entry_cancel",
                                        "contract_switch",
                                    )
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
                                previous_qff_symbol = qff_symbol
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
                                unsubscribe_qff_books_if_supported(
                                    qff_provider,
                                    previous_qff_symbol,
                                )
                                qff_symbol, qff_expiry, indicator, seed_bars = (
                                    switch_to_contract(
                                        store,
                                        self.config,
                                        strategy.state,
                                        eligible_contract,
                                        qff_provider=qff_provider,
                                        tsm_provider=tsm_provider,
                                        usdttwd_provider=usdttwd_provider,
                                        end=bar.timestamp,
                                    )
                                )
                                subscribe_qff_books_if_supported(
                                    qff_provider,
                                    qff_symbol,
                                    self.reporter,
                                    bar.timestamp,
                                )
                                builder = LiveMinuteBarBuilder(
                                    stale_seconds=self.config.live.stale_seconds,
                                    max_leg_timestamp_skew_seconds=(
                                        self.config.live.max_leg_timestamp_skew_seconds
                                    ),
                                )
                                builder.last_qff_close = seed_bars[-1].qff_close_filled
                                store.save_state(
                                    bar.row_index,
                                    bar.timestamp,
                                    strategy.state,
                                    indicator,
                                )
                                store.commit()
                                iterations += 1
                                if max_iterations is None or iterations < max_iterations:
                                    self.sleeper(self.config.live.polling_seconds)
                                continue
                            mark_pending_contract_switch_if_needed(
                                strategy.state,
                                eligible_contract,
                            )
                            if (
                                strategy.state.contract_policy_state
                                != bar.contract_policy_state
                            ):
                                bar = replace(
                                    bar,
                                    contract_policy_state=(
                                        strategy.state.contract_policy_state
                                    ),
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
                                self.reporter.warn(
                                    bar.timestamp,
                                    "missing_book",
                                    "skip_signal",
                                )
                            if should_force_exit_for_contract_policy(
                                self.config,
                                strategy.state,
                                bar.timestamp,
                            ):
                                result = strategy.force_exit(
                                    bar,
                                    decision_snapshot,
                                    exit_reason="rollover_force_exit",
                                )
                                self.reporter.event(
                                    bar.timestamp,
                                    "force_exit",
                                    "expiry_buffer",
                                )
                                store.record_event(
                                    bar.row_index,
                                    bar.timestamp,
                                    "rollover_force_exit",
                                    "forced exit before QFF expiry",
                                    {
                                        "qff_symbol": qff_symbol,
                                        "qff_expiry": qff_expiry,
                                    },
                                )
                                if result is None:
                                    result = strategy.on_bar(bar, decision_snapshot)
                            else:
                                result = strategy.on_bar(bar, decision_snapshot)
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
                            store.save_state(
                                bar.row_index,
                                bar.timestamp,
                                strategy.state,
                                indicator,
                            )
                            if (
                                strategy.state.state == StrategyState.FLAT
                                and strategy.state.pending_symbol_switch
                                and strategy.state.eligible_active_qff_symbol
                            ):
                                completed_contract = QffContractResolution(
                                    symbol=strategy.state.eligible_active_qff_symbol,
                                    expiry=strategy.state.eligible_active_qff_expiry,
                                    policy_state="active",
                                )
                                previous_qff_symbol = qff_symbol
                                unsubscribe_qff_books_if_supported(
                                    qff_provider,
                                    previous_qff_symbol,
                                )
                                qff_symbol, qff_expiry, indicator, seed_bars = (
                                    switch_to_contract(
                                        store,
                                        self.config,
                                        strategy.state,
                                        completed_contract,
                                        qff_provider=qff_provider,
                                        tsm_provider=tsm_provider,
                                        usdttwd_provider=usdttwd_provider,
                                        end=bar.timestamp,
                                    )
                                )
                                subscribe_qff_books_if_supported(
                                    qff_provider,
                                    qff_symbol,
                                    self.reporter,
                                    bar.timestamp,
                                )
                                builder = LiveMinuteBarBuilder(
                                    stale_seconds=self.config.live.stale_seconds,
                                    max_leg_timestamp_skew_seconds=(
                                        self.config.live.max_leg_timestamp_skew_seconds
                                    ),
                                )
                                builder.last_qff_close = seed_bars[-1].qff_close_filled
                                store.record_event(
                                    bar.row_index,
                                    bar.timestamp,
                                    "contract_switch_completed",
                                    "QFF contract switched after flat state",
                                    {"qff_symbol": qff_symbol},
                                )
                                self.reporter.event(
                                    bar.timestamp,
                                    "contract_switch_done",
                                    qff_symbol,
                                )
                                store.save_state(
                                    bar.row_index,
                                    bar.timestamp,
                                    strategy.state,
                                    indicator,
                                )
                            next_row_index += 1
                            bars_processed += 1
                    store.commit()

                iterations += 1
                if max_iterations is None or iterations < max_iterations:
                    self.sleeper(self.config.live.polling_seconds)

            if live_run_id is not None:
                store.finish_live_run(
                    live_run_id,
                    finished_at=ensure_taipei(self.clock()),
                    status="stopped",
                    payload={
                        "iterations": iterations,
                        "bars_processed": bars_processed,
                        "skipped_minutes": skipped_minutes,
                    },
                )
                store.commit()
                live_run_id = None
            return LivePaperResult(
                iterations=iterations,
                bars_processed=bars_processed,
                skipped_minutes=skipped_minutes,
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
            if qff_provider_to_close is not None:
                close = getattr(qff_provider_to_close, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass


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


def resolve_qff_symbol(config: AppConfig, provider: object) -> str:
    configured = config.live.qff_symbol
    if configured.lower() != "auto":
        return configured
    selector = getattr(provider, "select_front_month_symbol", None)
    if selector is None:
        raise RuntimeError("qff_symbol=auto requires a provider with front-month selector")
    return str(selector(config.live.qff_product))


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
