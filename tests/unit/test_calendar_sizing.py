from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from lux_trader.core.calendar import (
    TradingCalendar,
    is_weekend_force_exit_bar,
    live_session_status,
)
from lux_trader.core.fees import fill_costs
from lux_trader.core.models import Direction, MarketBar
from lux_trader.core.sizing import size_position_for_direction


TAIPEI = timezone.utc


def make_bar(index: int, timestamp: datetime, tw_leg_close: float | None = 100.0) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=timestamp,
        tw_leg_close=tw_leg_close,
        tw_leg_close_filled=100.0,
        us_leg_twd_fair=100.0,
        spread=0.0,
    )


def test_friday_night_is_close_only() -> None:
    friday_night = datetime.fromisoformat("2026-06-12T17:25:00+08:00")
    bars = TradingCalendar().annotate([make_bar(0, friday_night)])

    assert bars[0].close_allowed
    assert not bars[0].entry_allowed
    assert bars[0].friday_night_close_only


def test_weekend_session_is_close_only_and_marks_force_close() -> None:
    bars = TradingCalendar().annotate(
        [
            make_bar(0, datetime.fromisoformat("2026-06-12T13:43:00+08:00")),
            make_bar(1, datetime.fromisoformat("2026-06-12T17:25:00+08:00")),
            make_bar(2, datetime.fromisoformat("2026-06-12T17:26:00+08:00")),
            make_bar(3, datetime.fromisoformat("2026-06-15T08:45:00+08:00")),
        ]
    )

    assert bars[1].close_allowed
    assert not bars[1].entry_allowed
    assert bars[1].weekend_session_close_only
    assert bars[2].friday_session_end_force_close
    assert bars[3].entry_allowed


def test_live_calendar_closed_date_blocks_day_and_night_sessions() -> None:
    closed_dates = (date(2026, 6, 19),)

    friday_night = live_session_status(
        datetime.fromisoformat("2026-06-19T17:25:00+08:00"),
        closed_dates,
    )
    friday_after_midnight = live_session_status(
        datetime.fromisoformat("2026-06-19T02:30:00+08:00"),
        closed_dates,
    )
    saturday_after_midnight = live_session_status(
        datetime.fromisoformat("2026-06-20T02:30:00+08:00"),
        closed_dates,
    )

    assert not friday_night.is_trading
    assert friday_night.reason == "closed_date"
    assert not friday_after_midnight.is_trading
    assert friday_after_midnight.reason == "closed_date"
    assert not saturday_after_midnight.is_trading
    assert saturday_after_midnight.reason == "closed_date"
    assert saturday_after_midnight.next_open_at == datetime.fromisoformat(
        "2026-06-22T08:45:00+08:00"
    )


def test_live_calendar_weekday_sessions_and_friday_close_only() -> None:
    weekday_day = live_session_status(
        datetime.fromisoformat("2026-06-18T08:45:00+08:00"),
        (),
    )
    weekday_night = live_session_status(
        datetime.fromisoformat("2026-06-18T17:25:00+08:00"),
        (),
    )
    friday_night = live_session_status(
        datetime.fromisoformat("2026-06-12T17:25:00+08:00"),
        (),
    )

    assert weekday_day.is_trading
    assert not weekday_day.is_close_only
    assert weekday_night.is_trading
    assert not weekday_night.is_close_only
    assert friday_night.is_trading
    assert friday_night.is_close_only


def test_inactive_session_is_not_allowed_without_tw_leg_trades() -> None:
    timestamp = datetime.fromisoformat("2026-06-13T08:45:00+08:00")
    bars = TradingCalendar().annotate([make_bar(0, timestamp, tw_leg_close=None)])

    assert not bars[0].close_allowed
    assert not bars[0].entry_allowed


def test_weekend_force_exit_fires_in_grace_window_at_friday_session_end() -> None:
    # 2026-06-19 is a Friday; its night session runs into 2026-06-20 (Sat) 05:00,
    # after which QFF is frozen until Monday 2026-06-22.
    assert is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T04:57:00+08:00")
    )
    # Exactly grace_minutes (5) before the 05:00 end still counts; one minute more
    # does not.
    assert is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T04:55:00+08:00")
    )
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T04:54:00+08:00")
    )


def test_weekend_force_exit_ignores_start_of_friday_night_and_day_session() -> None:
    # Early in the Friday night session — far from the end — must not force-exit.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-19T17:30:00+08:00")
    )
    # Friday day session: the night session is still ahead this week.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-19T13:42:00+08:00")
    )


def test_weekend_force_exit_ignores_ordinary_weeknight_session_end() -> None:
    # Wednesday night -> Thursday 05:00: the Thursday day session follows in the
    # same ISO week, so this is not a weekend break.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-18T04:57:00+08:00")
    )


def test_weekend_force_exit_covers_monday_holiday_long_weekend() -> None:
    # 2026-06-22 (Mon) closed: the next trading session is Tuesday, still a new ISO
    # week, so the Friday-night flatten must still fire.
    assert is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T04:57:00+08:00"),
        (date(2026, 6, 22),),
    )


def test_weekend_force_exit_is_false_outside_trading_hours() -> None:
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T12:00:00+08:00")
    )


def test_position_sizing_direction_signs(strategy_config, fee_config) -> None:
    short_us_leg = size_position_for_direction(
        Direction.SHORT_US_LONG_TW,
        us_leg_price=2500.0,
        tw_leg_price=250.0,
        strategy=strategy_config,
        fees=fee_config,
    )
    long_us_leg = size_position_for_direction(
        Direction.LONG_US_SHORT_TW,
        us_leg_price=2500.0,
        tw_leg_price=250.0,
        strategy=strategy_config,
        fees=fee_config,
    )

    assert short_us_leg is not None
    assert long_us_leg is not None
    assert short_us_leg.tw_leg_contracts == 40
    assert short_us_leg.us_leg_units == pytest.approx(-80.0)
    assert short_us_leg.us_leg_units < 0
    assert short_us_leg.tw_leg_units > 0
    assert long_us_leg.us_leg_units == pytest.approx(80.0)
    assert long_us_leg.us_leg_units > 0
    assert long_us_leg.tw_leg_units < 0


def test_position_sizing_uses_binance_contract_quantity(strategy_config, fee_config) -> None:
    sizing = size_position_for_direction(
        Direction.SHORT_US_LONG_TW,
        us_leg_price=2880.31068,
        tw_leg_price=2487.5,
        strategy=replace_strategy_notional(strategy_config, 240_000.0),
        fees=fee_config,
    )

    assert sizing is not None
    assert sizing.tw_leg_contracts == 1
    assert sizing.actual_leg_notional_twd == pytest.approx(248_750.0)
    assert sizing.us_leg_units == pytest.approx(-17.27244229)


def test_position_sizing_can_use_fixed_tw_leg_lots(strategy_config, fee_config) -> None:
    sizing = size_position_for_direction(
        Direction.SHORT_US_LONG_TW,
        us_leg_price=2880.31068,
        tw_leg_price=2487.5,
        strategy=replace_strategy_tw_leg_lots(strategy_config, 2),
        fees=fee_config,
    )

    assert sizing is not None
    assert sizing.tw_leg_contracts == 2
    assert sizing.raw_tw_leg_contracts == 2.0
    assert sizing.actual_leg_notional_twd == pytest.approx(497_500.0)
    assert sizing.us_leg_units == pytest.approx(-34.54488459)


def test_fixed_tw_leg_lots_preserves_direction_signs(strategy_config, fee_config) -> None:
    sizing = size_position_for_direction(
        Direction.LONG_US_SHORT_TW,
        us_leg_price=2500.0,
        tw_leg_price=250.0,
        strategy=replace_strategy_tw_leg_lots(strategy_config, 3),
        fees=fee_config,
    )

    assert sizing is not None
    assert sizing.tw_leg_contracts == -3
    assert sizing.tw_leg_units == -300.0
    assert sizing.us_leg_units == pytest.approx(6.0)


def test_us_leg_fee_uses_binance_contract_twd_price(fee_config) -> None:
    costs = fill_costs(
        us_leg_units=-17.27244229,
        us_leg_price=2880.31068,
        tw_leg_contracts=1,
        tw_leg_price=2487.5,
        fees=fee_config,
    )

    assert costs["us_leg_fee_twd"] == pytest.approx(124.375)


def replace_strategy_notional(strategy_config, leg_notional_twd: float):
    return strategy_config.__class__(
        entry_z=strategy_config.entry_z,
        exit_z=strategy_config.exit_z,
        leg_notional_twd=leg_notional_twd,
        initial_capital_twd=strategy_config.initial_capital_twd,
        max_entry_delay_minutes=strategy_config.max_entry_delay_minutes,
        zscore_window=strategy_config.zscore_window,
        tw_leg_lots=strategy_config.tw_leg_lots,
    )


def replace_strategy_tw_leg_lots(strategy_config, tw_leg_lots: int):
    return strategy_config.__class__(
        entry_z=strategy_config.entry_z,
        exit_z=strategy_config.exit_z,
        leg_notional_twd=strategy_config.leg_notional_twd,
        initial_capital_twd=strategy_config.initial_capital_twd,
        max_entry_delay_minutes=strategy_config.max_entry_delay_minutes,
        zscore_window=strategy_config.zscore_window,
        tw_leg_lots=tw_leg_lots,
    )
