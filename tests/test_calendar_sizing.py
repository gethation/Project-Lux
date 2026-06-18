from __future__ import annotations

from datetime import datetime, timezone

from lux_trader.calendar import TradingCalendar
from lux_trader.models import Direction, MarketBar
from lux_trader.sizing import size_position_for_direction


TAIPEI = timezone.utc


def make_bar(index: int, timestamp: datetime, qff_close: float | None = 100.0) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=timestamp,
        qff_close=qff_close,
        qff_close_filled=100.0,
        tsm_twd_fair=100.0,
        spread=0.0,
    )


def test_friday_night_is_close_only() -> None:
    friday_night = datetime.fromisoformat("2026-06-12T17:25:00+08:00")
    bars = TradingCalendar().annotate([make_bar(0, friday_night)])

    assert bars[0].close_allowed
    assert not bars[0].entry_allowed
    assert bars[0].friday_night_close_only


def test_inactive_session_is_not_allowed_without_qff_trades() -> None:
    timestamp = datetime.fromisoformat("2026-06-13T08:45:00+08:00")
    bars = TradingCalendar().annotate([make_bar(0, timestamp, qff_close=None)])

    assert not bars[0].close_allowed
    assert not bars[0].entry_allowed


def test_position_sizing_direction_signs(strategy_config, fee_config) -> None:
    short_tsm = size_position_for_direction(
        Direction.SHORT_TSM_LONG_QFF,
        tsm_price=2500.0,
        qff_price=250.0,
        strategy=strategy_config,
        fees=fee_config,
    )
    long_tsm = size_position_for_direction(
        Direction.LONG_TSM_SHORT_QFF,
        tsm_price=2500.0,
        qff_price=250.0,
        strategy=strategy_config,
        fees=fee_config,
    )

    assert short_tsm is not None
    assert long_tsm is not None
    assert short_tsm.qff_contracts == 40
    assert short_tsm.tsm_units < 0
    assert short_tsm.qff_units > 0
    assert long_tsm.tsm_units > 0
    assert long_tsm.qff_units < 0
