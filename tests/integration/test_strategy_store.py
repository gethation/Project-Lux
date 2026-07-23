from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.brokers import PaperBroker
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.models import (
    BrokerName,
    Direction,
    IndicatorSnapshot,
    MarketBar,
    OrderSide,
    StrategyAction,
    StrategyState,
)
from lux_trader.store import SQLiteStore
from lux_trader.core.strategy import PairStrategy, StrategyRuntimeState


def make_bar(index: int, timestamp: str, entry_allowed: bool = True, close_allowed: bool = True) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=datetime.fromisoformat(timestamp),
        tw_leg_close=250.0,
        tw_leg_close_filled=250.0,
        us_leg_twd_fair=100.0 + index,
        spread=0.0,
        entry_allowed=entry_allowed,
        close_allowed=close_allowed,
        tw_leg_symbol="QFFG6",
        tw_leg_expiry="2026-02-18",
        contract_policy_state="active",
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
    strategy = PairStrategy(
        strategy_config,
        fee_config,
        PaperBroker(),
        us_leg_symbol="TSM/USDT:USDT",
        tw_leg_symbol="QFF",
        tw_leg_contract_multiplier=100.0,
        us_leg_contract_multiplier=5.0,
    )
    bar0 = make_bar(0, "2026-06-08T08:45:00+08:00")
    bar1 = make_bar(1, "2026-06-08T08:46:00+08:00")
    bar2 = make_bar(2, "2026-06-08T08:47:00+08:00")
    bar3 = make_bar(3, "2026-06-08T08:48:00+08:00")

    result0 = strategy.on_bar(bar0, make_snapshot(bar0, 2.1))
    result1 = strategy.on_bar(bar1, make_snapshot(bar1, 1.0))
    result2 = strategy.on_bar(bar2, make_snapshot(bar2, -1.1))
    result3 = strategy.on_bar(bar3, make_snapshot(bar3, -1.2))

    assert result0.action == StrategyAction.ENTRY_SIGNAL
    assert result1.action == StrategyAction.ENTRY_FILL
    assert result1.reason == "entry_filled"
    assert result2.action == StrategyAction.EXIT_SIGNAL
    assert result3.action == StrategyAction.EXIT_FILL
    assert result3.trade is not None
    assert result3.trade["us_leg_units"] == pytest.approx(-1_000_000.0 / (101.0 * 5.0))
    assert result3.trade["us_leg_pnl"] == pytest.approx(
        (-1_000_000.0 / (101.0 * 5.0)) * ((103.0 - 101.0) * 5.0)
    )
    assert strategy.state.state == StrategyState.FLAT


def test_entry_delay_exceeded_cancels_pending(strategy_config, fee_config) -> None:
    strategy = PairStrategy(
        strategy_config,
        fee_config,
        PaperBroker(),
        us_leg_symbol="TSM/USDT:USDT",
        tw_leg_symbol="QFF",
        tw_leg_contract_multiplier=100.0,
        us_leg_contract_multiplier=5.0,
    )
    day_close = make_bar(0, "2026-06-08T13:45:00+08:00")
    night_open = make_bar(1, "2026-06-08T17:25:00+08:00")

    result0 = strategy.on_bar(day_close, make_snapshot(day_close, 2.1))
    result1 = strategy.on_bar(night_open, make_snapshot(night_open, 1.0))

    assert result0.action == StrategyAction.ENTRY_SIGNAL
    assert result1.action == StrategyAction.ENTRY_CANCEL
    assert strategy.state.state == StrategyState.FLAT


def test_strategy_builds_entry_order_requests_without_submitting(
    strategy_config,
    fee_config,
) -> None:
    strategy = PairStrategy(
        strategy_config,
        fee_config,
        PaperBroker(),
        us_leg_symbol="CUSTOM/USDT:USDT",
        tw_leg_symbol="QFF",
        tw_leg_contract_multiplier=100.0,
        us_leg_contract_multiplier=5.0,
    )
    bar = make_bar(10, "2026-06-08T08:55:00+08:00")

    requests = strategy.build_entry_order_requests(
        bar=bar,
        us_leg_units=-125.5,
        tw_leg_contracts=3,
        costs={
            "us_leg_fee_twd": 12.3,
            "tw_leg_fee_twd": 15.0,
            "tw_leg_tax_twd": 1.5,
        },
    )

    assert len(requests) == 2
    us_leg_request, tw_leg_request = requests
    assert us_leg_request.broker == BrokerName.BINANCE
    assert us_leg_request.symbol == "CUSTOM/USDT:USDT"
    assert us_leg_request.side == OrderSide.SELL
    assert us_leg_request.quantity == 125.5
    assert us_leg_request.fee_twd == 12.3
    assert tw_leg_request.broker == BrokerName.FUBON
    assert tw_leg_request.symbol == "QFFG6"
    assert tw_leg_request.side == OrderSide.BUY
    assert tw_leg_request.quantity == 3
    assert tw_leg_request.fee_twd == 16.5
    assert tw_leg_request.tw_leg_expiry == "2026-02-18"
    assert tw_leg_request.contract_policy_state == "active"


def test_strategy_builds_exit_order_requests_from_open_state(
    strategy_config,
    fee_config,
) -> None:
    state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_US_LONG_TW,
        us_leg_units=-125.5,
        tw_leg_contracts=3,
    )
    strategy = PairStrategy(
        strategy_config,
        fee_config,
        PaperBroker(),
        state=state,
        us_leg_symbol="TSM/USDT:USDT",
        tw_leg_symbol="QFF",
        tw_leg_contract_multiplier=100.0,
        us_leg_contract_multiplier=5.0,
    )
    bar = make_bar(11, "2026-06-08T08:56:00+08:00")

    requests = strategy.build_exit_order_requests(
        bar=bar,
        costs={
            "us_leg_fee_twd": 12.3,
            "tw_leg_fee_twd": 15.0,
            "tw_leg_tax_twd": 1.5,
        },
    )

    us_leg_request, tw_leg_request = requests
    assert us_leg_request.side == OrderSide.BUY
    assert us_leg_request.quantity == 125.5
    assert tw_leg_request.side == OrderSide.SELL
    assert tw_leg_request.quantity == 3


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
