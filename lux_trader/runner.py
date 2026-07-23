from __future__ import annotations

from dataclasses import dataclass

from .brokers import PaperBroker
from .config import AppConfig
from .core.indicator import IndicatorEngine, validate_expected_zscore
from .market_data import CsvReplayMarketData
from .store import SQLiteStore
from .core.strategy import PairStrategy, StrategyRuntimeState


@dataclass(frozen=True)
class ReplayResult:
    rows_processed: int
    start_row: int | None
    end_row: int | None
    finalized: bool


class SystemRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def replay(
        self,
        *,
        max_bars: int | None = None,
        resume: bool = False,
        reset_store: bool = False,
    ) -> ReplayResult:
        store = SQLiteStore(
            self.config.store_path,
            **self.config.store_identity(),
        )
        try:
            if reset_store:
                store.reset()
            store.initialize()
            if not resume and not reset_store and store.has_bars():
                raise RuntimeError(
                    "Store already has replay data. Use --resume or --reset-store."
                )

            resume_state = store.load_resume_state() if resume else None
            if resume and resume_state is None and store.has_bars():
                raise RuntimeError("Store has bars but no strategy_state row")

            bars = CsvReplayMarketData(
                self.config.input_csv,
                tw_leg_ohlcv_path=self.config.tw_leg_ohlcv_csv,
                us_leg_ohlcv_path=self.config.us_leg_ohlcv_csv,
                usdttwd_ohlcv_path=self.config.usdttwd_ohlcv_csv,
            ).load()
            if resume_state is None:
                indicator = IndicatorEngine(window=self.config.strategy.zscore_window)
                strategy_state = StrategyRuntimeState(
                    running_max_equity=self.config.strategy.initial_capital_twd
                )
                start_index = 0
            else:
                indicator = IndicatorEngine(window=self.config.strategy.zscore_window)
                strategy_state = resume_state.strategy
                start_index = resume_state.row_index + 1
                for warm_bar in bars[:start_index]:
                    indicator.update(warm_bar)

            broker = PaperBroker()
            strategy = PairStrategy(
                self.config.strategy,
                self.config.fees,
                broker,
                state=strategy_state,
                us_leg_symbol=self.config.live.binance_symbol,
                tw_leg_symbol=self.config.active_pair.tw_leg.product,
                tw_leg_contract_multiplier=(
                    self.config.active_pair.tw_leg.contract_multiplier
                ),
                us_leg_contract_multiplier=(
                    self.config.active_pair.us_leg.adr_share_ratio
                ),
            )

            rows_processed = 0
            first_row: int | None = None
            last_row: int | None = None
            last_bar = None
            last_snapshot = None

            for bar in bars[start_index:]:
                if max_bars is not None and rows_processed >= max_bars:
                    break
                snapshot = indicator.update(bar)
                if self.config.safety.validate_expected_zscore:
                    validate_expected_zscore(
                        bar,
                        snapshot,
                        self.config.safety.expected_zscore_tolerance,
                    )

                result = strategy.on_bar(bar, snapshot)
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
                )
                store.save_state(bar.row_index, bar.timestamp, strategy.state, indicator)
                store.commit()

                first_row = bar.row_index if first_row is None else first_row
                last_row = bar.row_index
                last_bar = bar
                last_snapshot = snapshot
                rows_processed += 1

            finalized = False
            reached_end = last_row is not None and last_row == bars[-1].row_index
            if reached_end and last_bar is not None and last_snapshot is not None:
                final_result = strategy.finalize(last_bar, last_snapshot)
                if final_result is not None:
                    for order in final_result.orders:
                        store.record_order(order)
                    for fill in final_result.fills:
                        store.record_fill(fill)
                    if final_result.trade is not None:
                        store.record_trade(final_result.trade)
                    store.record_event(
                        last_bar.row_index,
                        last_bar.timestamp,
                        final_result.action.value,
                        final_result.reason,
                        {"state": strategy.state.state.value},
                    )
                    store.record_bar(
                        last_bar,
                        last_snapshot,
                        strategy.state,
                        final_result.unrealized_pnl,
                        final_result.equity,
                        final_result.running_max_equity,
                        final_result.drawdown_twd,
                        final_result.drawdown_pct,
                    )
                    store.save_state(
                        last_bar.row_index,
                        last_bar.timestamp,
                        strategy.state,
                        indicator,
                    )
                    store.commit()
                    finalized = True

            return ReplayResult(
                rows_processed=rows_processed,
                start_row=first_row,
                end_row=last_row,
                finalized=finalized,
            )
        finally:
            store.close()
