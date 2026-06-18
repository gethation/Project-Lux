from __future__ import annotations

from datetime import datetime

from lux_trader.brokers import PaperBroker
from lux_trader.indicator import IndicatorEngine
from lux_trader.models import IndicatorSnapshot, MarketBar, StrategyAction, StrategyState
from lux_trader.store import SQLiteStore
from lux_trader.strategy import PairStrategy, StrategyRuntimeState


def make_bar(index: int, timestamp: str, entry_allowed: bool = True, close_allowed: bool = True) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=datetime.fromisoformat(timestamp),
        qff_close=250.0,
        qff_close_filled=250.0,
        tsm_twd_fair=100.0 + index,
        spread=0.0,
        entry_allowed=entry_allowed,
        close_allowed=close_allowed,
    )


def make_snapshot(bar: MarketBar, zscore: float) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        timestamp=bar.timestamp,
        spread=bar.spread,
        mean=0.0,
        std=1.0,
        zscore=zscore,
        zscore_valid=True,
        entry_allowed=bar.entry_allowed,
        close_allowed=bar.close_allowed,
        friday_night_close_only=bar.friday_night_close_only,
    )


def test_strategy_entry_open_exit_cycle(strategy_config, fee_config) -> None:
    strategy = PairStrategy(strategy_config, fee_config, PaperBroker())
    bar0 = make_bar(0, "2026-06-08T08:45:00+08:00")
    bar1 = make_bar(1, "2026-06-08T08:46:00+08:00")
    bar2 = make_bar(2, "2026-06-08T08:47:00+08:00")
    bar3 = make_bar(3, "2026-06-08T08:48:00+08:00")

    result0 = strategy.on_bar(bar0, make_snapshot(bar0, 2.1))
    result1 = strategy.on_bar(bar1, make_snapshot(bar1, 1.0))
    result2 = strategy.on_bar(bar2, make_snapshot(bar2, -0.1))
    result3 = strategy.on_bar(bar3, make_snapshot(bar3, -0.2))

    assert result0.action == StrategyAction.ENTRY_SIGNAL
    assert result1.action == StrategyAction.ENTRY_FILL
    assert strategy.state.state == StrategyState.FLAT
    assert result2.action == StrategyAction.EXIT_SIGNAL
    assert result3.action == StrategyAction.EXIT_FILL
    assert result3.trade is not None


def test_entry_delay_exceeded_cancels_pending(strategy_config, fee_config) -> None:
    strategy = PairStrategy(strategy_config, fee_config, PaperBroker())
    day_close = make_bar(0, "2026-06-08T13:45:00+08:00")
    night_open = make_bar(1, "2026-06-08T17:25:00+08:00")

    result0 = strategy.on_bar(day_close, make_snapshot(day_close, 2.1))
    result1 = strategy.on_bar(night_open, make_snapshot(night_open, 1.0))

    assert result0.action == StrategyAction.ENTRY_SIGNAL
    assert result1.action == StrategyAction.ENTRY_CANCEL
    assert strategy.state.state == StrategyState.FLAT


def test_sqlite_state_roundtrip(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    try:
        store.initialize()
        state = StrategyRuntimeState(state=StrategyState.ENTRY_PENDING, candidate_idx=10)
        indicator = IndicatorEngine(window=3)
        indicator.update(make_bar(0, "2026-06-08T08:45:00+08:00"))
        store.save_state(10, datetime.fromisoformat("2026-06-08T08:55:00+08:00"), state, indicator)
        store.commit()

        restored = store.load_resume_state()
        assert restored is not None
        assert restored.row_index == 10
        assert restored.strategy.state == StrategyState.ENTRY_PENDING
        assert restored.strategy.candidate_idx == 10
        assert restored.indicator.window == 3
        assert list(restored.indicator.values) == []
    finally:
        store.close()
