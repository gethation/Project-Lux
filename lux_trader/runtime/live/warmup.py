from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from lux_trader.integrations.binance.execution import BinanceUsLegExecutionAdapter
from lux_trader.config import AppConfig
from lux_trader.core.contract_policy import ExpiryBufferContractPolicy, TwLegContractSelection
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
from lux_trader.integrations.fubon.market_data import FubonTwLegMarketData
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.taifex.downloader import TaifexTwLegTradeDownloader
from lux_trader.core.fees import fill_costs
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.execution.gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from lux_trader.market_data import (
    CsvTwLegWarmupProvider,
    LiveMinuteBarBuilder,
    LiveQuoteSet,
    OhlcvProvider,
    QFF_FORWARD_FILL_LOOKBACK,
    TwLegWarmupSourceReport,
    TwLegWarmupProvider,
    QuoteProvider,
    WarmupBuilder,
    build_tw_leg_expected_warmup_index,
    build_tw_leg_warmup_source_report,
    floor_minute,
    parse_timestamp,
    validate_tw_leg_warmup_report,
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

from lux_trader.runtime.live.contracts import resolve_tw_leg_contract


@dataclass(frozen=True)
class WarmupResult:
    bars_written: int
    tw_leg_symbol: str
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class TwLegWarmupCheckResult:
    tw_leg_symbol: str
    tw_leg_expiry: str | None
    contract_policy_state: str
    start: datetime
    end: datetime
    tw_leg_fetch_start: datetime
    report: TwLegWarmupSourceReport
    output_csv: str | None


class WarmupRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        tw_leg_provider: FubonTwLegMarketData | None = None,
        tw_leg_fallback_provider: TwLegWarmupProvider | None = None,
        us_leg_provider: OhlcvProvider | None = None,
        usdttwd_provider: OhlcvProvider | None = None,
    ) -> None:
        self.config = config
        self.tw_leg_provider = tw_leg_provider
        self.tw_leg_fallback_provider = tw_leg_fallback_provider
        self.us_leg_provider = us_leg_provider
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
            tw_leg_provider = self.tw_leg_provider or FubonTwLegMarketData(
                self.config.live.fubon_env_path
            )
            contract = resolve_tw_leg_contract(self.config, tw_leg_provider)
            fallback = self.tw_leg_fallback_provider
            if fallback is None and self.config.live.taifex_use_network:
                fallback = TaifexTwLegTradeDownloader(self.config.live.taifex_cache_dir)
            elif fallback is None and self.config.live.taifex_tw_leg_1m_csv is not None:
                fallback = CsvTwLegWarmupProvider(self.config.live.taifex_tw_leg_1m_csv)
            us_leg_provider = self.us_leg_provider or BinanceMarketData()
            usdttwd_provider = self.usdttwd_provider or BitoProMarketData()
            builder = WarmupBuilder(
                live_config=self.config.live,
                tw_leg_intraday_provider=tw_leg_provider,
                tw_leg_fallback_provider=fallback,
                us_leg_provider=us_leg_provider,
                usdttwd_provider=usdttwd_provider,
                closed_dates=self.config.trading_calendar.closed_dates,
            )
            bars = builder.build(
                tw_leg_symbol=contract.symbol,
                tw_leg_expiry=contract.expiry,
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
                    "tw_leg_symbol": contract.symbol,
                    "tw_leg_expiry": contract.expiry,
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
                tw_leg_symbol=contract.symbol,
                start=bars[0].timestamp if bars else None,
                end=bars[-1].timestamp if bars else None,
            )
        finally:
            store.close()


class TwLegWarmupCheckRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        tw_leg_provider: FubonTwLegMarketData | None = None,
        taifex_provider: TwLegWarmupProvider | None = None,
    ) -> None:
        self.config = config
        self.tw_leg_provider = tw_leg_provider
        self.taifex_provider = taifex_provider

    def run(
        self,
        *,
        output_csv: str | None = None,
        end: datetime | None = None,
    ) -> TwLegWarmupCheckResult:
        if self.config.safety.allow_live_order:
            raise RuntimeError("Refusing tw_leg-warmup-check with allow_live_order=true")

        tw_leg_provider = self.tw_leg_provider or FubonTwLegMarketData(
            self.config.live.fubon_env_path
        )
        try:
            contract = resolve_tw_leg_contract(self.config, tw_leg_provider)
            end_minute = floor_minute(end or datetime.now().astimezone()) - timedelta(
                minutes=1
            )
            tw_leg_fetch_start = end_minute - QFF_FORWARD_FILL_LOOKBACK
            taifex_provider = self.taifex_provider or TaifexTwLegTradeDownloader(
                self.config.live.taifex_cache_dir
            )
            taifex_frame = taifex_provider.fetch_1m(
                contract.symbol, tw_leg_fetch_start, end_minute
            )
            fubon_frame = tw_leg_provider.fetch_1m(
                contract.symbol, tw_leg_fetch_start, end_minute
            )
            if taifex_frame.empty:
                raise RuntimeError("TAIFEX QFF warmup data is empty")
            if fubon_frame.empty:
                raise RuntimeError("Fubon QFF intraday candles are empty")
            warmup_index, session_index = build_tw_leg_expected_warmup_index(
                start=tw_leg_fetch_start,
                end=end_minute,
                count=self.config.live.warmup_minutes,
                closed_dates=self.config.trading_calendar.closed_dates,
            )
            start_minute = warmup_index[0].to_pydatetime()

            report = build_tw_leg_warmup_source_report(
                [("taifex", taifex_frame), ("fubon", fubon_frame)],
                start_minute=start_minute,
                end_minute=end_minute,
                tw_leg_fetch_start=tw_leg_fetch_start,
                warmup_index=warmup_index,
                fill_index=session_index,
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
            validate_tw_leg_warmup_report(
                report,
                max_trailing_fill_minutes=(
                    self.config.live.warmup_tw_leg_max_trailing_fill_minutes
                ),
                max_forward_fill_ratio=(
                    self.config.live.warmup_forward_fill_max_ratio
                ),
            )

            resolved_output = self._resolve_output_csv(output_csv)
            if resolved_output is not None:
                resolved_output.parent.mkdir(parents=True, exist_ok=True)
                report.frame.to_csv(resolved_output, index=False)

            return TwLegWarmupCheckResult(
                tw_leg_symbol=contract.symbol,
                tw_leg_expiry=contract.expiry,
                contract_policy_state=contract.policy_state,
                start=start_minute,
                end=end_minute,
                tw_leg_fetch_start=tw_leg_fetch_start,
                report=report,
                output_csv=str(resolved_output) if resolved_output is not None else None,
            )
        finally:
            if self.tw_leg_provider is None:
                tw_leg_provider.close()

    def _resolve_output_csv(self, output_csv: str | None) -> Path | None:
        if output_csv == "":
            return None
        if output_csv is not None:
            return Path(output_csv)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.config.store_path.parent / f"tw_leg_warmup_check_{timestamp}.csv"


def load_or_build_live_indicator(
    store: SQLiteStore,
    config: AppConfig,
    *,
    tw_leg_symbol: str,
    tw_leg_expiry: str | None,
    policy_state: str,
    tw_leg_provider: TwLegWarmupProvider,
    us_leg_provider: OhlcvProvider,
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
            tw_leg_symbol=tw_leg_symbol,
        )
    if len(seed_bars) < config.strategy.zscore_window:
        if not allow_rebuild:
            raise RuntimeError(
                "Warmup seed is missing or insufficient for live startup: "
                f"found {len(seed_bars)} bars for {tw_leg_symbol}, "
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
                    "tw_leg_symbol": tw_leg_symbol,
                    "tw_leg_expiry": tw_leg_expiry,
                    "contract_policy_state": policy_state,
                    "existing_seed_bars": len(seed_bars),
                    "required_seed_bars": config.strategy.zscore_window,
                    "context": auto_warmup_context,
                },
            )
        fallback: TwLegWarmupProvider | None
        if config.live.taifex_use_network:
            fallback = TaifexTwLegTradeDownloader(config.live.taifex_cache_dir)
        elif config.live.taifex_tw_leg_1m_csv is not None:
            fallback = CsvTwLegWarmupProvider(config.live.taifex_tw_leg_1m_csv)
        else:
            fallback = None
        builder = WarmupBuilder(
            live_config=config.live,
            tw_leg_intraday_provider=tw_leg_provider,
            tw_leg_fallback_provider=fallback,
            us_leg_provider=us_leg_provider,
            usdttwd_provider=usdttwd_provider,
            closed_dates=config.trading_calendar.closed_dates,
        )
        seed_bars = builder.build(
            tw_leg_symbol=tw_leg_symbol,
            tw_leg_expiry=tw_leg_expiry,
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
                    "tw_leg_symbol": tw_leg_symbol,
                    "tw_leg_expiry": tw_leg_expiry,
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

