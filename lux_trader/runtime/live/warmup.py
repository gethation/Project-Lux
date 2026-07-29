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
from lux_trader.integrations.fubon.execution import FubonFutureExecutionAdapter
from lux_trader.integrations.fubon.market_data import FubonCcfMarketData
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.integrations.taifex.downloader import TaifexCcfTradeDownloader
from lux_trader.integrations.venues import (
    open_fx_quote_provider,
    open_umc_quote_provider,
)
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
    build_ccf_expected_warmup_index,
    build_ccf_warmup_source_report,
    floor_minute,
    parse_timestamp,
    validate_ccf_warmup_report,
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

from lux_trader.runtime.live.contracts import resolve_ccf_contract


@dataclass(frozen=True)
class WarmupResult:
    bars_written: int
    ccf_symbol: str
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class CcfWarmupCheckResult:
    ccf_symbol: str
    ccf_expiry: str | None
    contract_policy_state: str
    start: datetime
    end: datetime
    ccf_fetch_start: datetime
    report: CcfWarmupSourceReport
    output_csv: str | None


class WarmupRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        ccf_provider: FubonCcfMarketData | None = None,
        ccf_fallback_provider: CcfWarmupProvider | None = None,
        umc_provider: OhlcvProvider | None = None,
        usd_twd_provider: OhlcvProvider | None = None,
    ) -> None:
        self.config = config
        self.ccf_provider = ccf_provider
        self.ccf_fallback_provider = ccf_fallback_provider
        self.umc_provider = umc_provider
        self.usd_twd_provider = usd_twd_provider

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
            ccf_provider = self.ccf_provider or FubonCcfMarketData(
                self.config.live.fubon_env_path
            )
            contract = resolve_ccf_contract(self.config, ccf_provider)
            fallback = self.ccf_fallback_provider
            if fallback is None and self.config.live.taifex_use_network:
                fallback = TaifexCcfTradeDownloader(self.config.live.taifex_cache_dir)
            elif fallback is None and self.config.live.taifex_ccf_1m_csv is not None:
                fallback = CsvCcfWarmupProvider(self.config.live.taifex_ccf_1m_csv)
            umc_provider = self.umc_provider or open_umc_quote_provider(self.config)
            usd_twd_provider = self.usd_twd_provider or open_fx_quote_provider(
                self.config
            )
            builder = WarmupBuilder(
                live_config=self.config.live,
                ccf_intraday_provider=ccf_provider,
                ccf_fallback_provider=fallback,
                umc_provider=umc_provider,
                usd_twd_provider=usd_twd_provider,
                closed_dates=self.config.trading_calendar.closed_dates,
            )
            bars = builder.build(
                ccf_symbol=contract.symbol,
                ccf_expiry=contract.expiry,
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
                    "ccf_symbol": contract.symbol,
                    "ccf_expiry": contract.expiry,
                    "contract_policy_state": contract.policy_state,
                    "start_timestamp": bars[0].timestamp.isoformat(),
                    "end_timestamp": bars[-1].timestamp.isoformat(),
                    "requested_end": ensure_taipei(
                        end or datetime.now().astimezone()
                    ).isoformat(),
                },
            )
            store.commit()
            return WarmupResult(
                bars_written=len(bars),
                ccf_symbol=contract.symbol,
                start=bars[0].timestamp if bars else None,
                end=bars[-1].timestamp if bars else None,
            )
        finally:
            store.close()


class CcfWarmupCheckRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        ccf_provider: FubonCcfMarketData | None = None,
        taifex_provider: CcfWarmupProvider | None = None,
    ) -> None:
        self.config = config
        self.ccf_provider = ccf_provider
        self.taifex_provider = taifex_provider

    def run(
        self,
        *,
        output_csv: str | None = None,
        end: datetime | None = None,
    ) -> CcfWarmupCheckResult:
        if self.config.safety.allow_live_order:
            raise RuntimeError("Refusing ccf-warmup-check with allow_live_order=true")

        ccf_provider = self.ccf_provider or FubonCcfMarketData(
            self.config.live.fubon_env_path
        )
        try:
            contract = resolve_ccf_contract(self.config, ccf_provider)
            end_minute = floor_minute(end or datetime.now().astimezone()) - timedelta(
                minutes=1
            )
            ccf_fetch_start = end_minute - CCF_FORWARD_FILL_LOOKBACK
            taifex_provider = self.taifex_provider or TaifexCcfTradeDownloader(
                self.config.live.taifex_cache_dir
            )
            taifex_frame = taifex_provider.fetch_1m(
                contract.symbol, ccf_fetch_start, end_minute
            )
            fubon_frame = ccf_provider.fetch_1m(
                contract.symbol, ccf_fetch_start, end_minute
            )
            if taifex_frame.empty:
                raise RuntimeError("TAIFEX CCF warmup data is empty")
            if fubon_frame.empty:
                raise RuntimeError("Fubon CCF intraday candles are empty")
            warmup_index, session_index = build_ccf_expected_warmup_index(
                start=ccf_fetch_start,
                end=end_minute,
                count=self.config.live.warmup_minutes,
                closed_dates=self.config.trading_calendar.closed_dates,
            )
            start_minute = warmup_index[0].to_pydatetime()

            report = build_ccf_warmup_source_report(
                [("taifex", taifex_frame), ("fubon", fubon_frame)],
                start_minute=start_minute,
                end_minute=end_minute,
                ccf_fetch_start=ccf_fetch_start,
                warmup_index=warmup_index,
                fill_index=session_index,
            )
            if len(report.frame) != self.config.live.warmup_minutes:
                raise RuntimeError(
                    f"CCF warmup report has {len(report.frame)} rows, "
                    f"need {self.config.live.warmup_minutes}"
                )
            if report.null_count:
                raise RuntimeError(
                    f"CCF warmup report has {report.null_count} null filled closes"
                )
            validate_ccf_warmup_report(
                report,
                max_trailing_fill_minutes=(
                    self.config.live.warmup_ccf_max_trailing_fill_minutes
                ),
                max_forward_fill_ratio=(
                    self.config.live.warmup_forward_fill_max_ratio
                ),
            )

            resolved_output = self._resolve_output_csv(output_csv)
            if resolved_output is not None:
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                report.frame.to_csv(resolved_output, index=False)

            return CcfWarmupCheckResult(
                ccf_symbol=contract.symbol,
                ccf_expiry=contract.expiry,
                contract_policy_state=contract.policy_state,
                start=start_minute,
                end=end_minute,
                ccf_fetch_start=ccf_fetch_start,
                report=report,
                output_csv=str(resolved_output) if resolved_output is not None else None,
            )
        finally:
            if self.ccf_provider is None:
                ccf_provider.close()

    def _resolve_output_csv(self, output_csv: str | None) -> Path | None:
        if output_csv == "":
            return None
        if output_csv is not None:
            return Path(output_csv)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.config.store_path.parent / f"ccf_warmup_check_{timestamp}.csv"


def load_or_build_live_indicator(
    store: SQLiteStore,
    config: AppConfig,
    *,
    ccf_symbol: str,
    ccf_expiry: str | None,
    policy_state: str,
    ccf_provider: CcfWarmupProvider,
    umc_provider: OhlcvProvider,
    usd_twd_provider: OhlcvProvider,
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
            ccf_symbol=ccf_symbol,
        )
    if len(seed_bars) < config.strategy.zscore_window:
        if not allow_rebuild:
            raise RuntimeError(
                "Warmup seed is missing or insufficient for live startup: "
                f"found {len(seed_bars)} bars for {ccf_symbol}, "
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
                "auto warmup started",
                {
                    "ccf_symbol": ccf_symbol,
                    "ccf_expiry": ccf_expiry,
                    "contract_policy_state": policy_state,
                    "existing_seed_bars": len(seed_bars),
                    "required_seed_bars": config.strategy.zscore_window,
                    "context": auto_warmup_context,
                },
            )
        fallback: CcfWarmupProvider | None
        if config.live.taifex_use_network:
            fallback = TaifexCcfTradeDownloader(config.live.taifex_cache_dir)
        elif config.live.taifex_ccf_1m_csv is not None:
            fallback = CsvCcfWarmupProvider(config.live.taifex_ccf_1m_csv)
        else:
            fallback = None
        builder = WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=ccf_provider,
            ccf_fallback_provider=fallback,
            umc_provider=umc_provider,
            usd_twd_provider=usd_twd_provider,
            closed_dates=config.trading_calendar.closed_dates,
        )
        seed_bars = builder.build(
            ccf_symbol=ccf_symbol,
            ccf_expiry=ccf_expiry,
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
                reporter.event(
                    end,
                    "warmup_auto",
                    f"done_{len(seed_bars)} "
                    f"start={seed_bars[0].timestamp.isoformat()} "
                    f"end={seed_bars[-1].timestamp.isoformat()}",
                )
            store.record_event(
                seed_bars[-1].row_index,
                seed_bars[-1].timestamp,
                "warmup_auto_before_live",
                "auto warmup bars written",
                {
                    "bars": len(seed_bars),
                    "ccf_symbol": ccf_symbol,
                    "ccf_expiry": ccf_expiry,
                    "contract_policy_state": policy_state,
                    "context": auto_warmup_context,
                    "force_rebuild": force_rebuild,
                    "start_timestamp": seed_bars[0].timestamp.isoformat(),
                    "end_timestamp": seed_bars[-1].timestamp.isoformat(),
                    "requested_end": end.isoformat(),
                },
            )

    indicator = IndicatorEngine(window=config.strategy.zscore_window)
    for seed_bar in seed_bars:
        indicator.update(seed_bar)
    return indicator, seed_bars

