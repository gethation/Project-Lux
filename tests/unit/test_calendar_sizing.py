from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from lux_trader.core.calendar import (
    TradingCalendar,
    is_weekend_force_exit_bar,
    live_session_status,
    taifex_session_status,
)
from lux_trader.config import FeeConfig, StrategyConfig, load_config
from lux_trader.core.fees import fill_costs
from lux_trader.core.models import Direction, MarketBar
from lux_trader.core.sizing import size_position_for_direction


TAIPEI = timezone.utc


def make_bar(index: int, timestamp: datetime, ccf_close: float | None = 100.0) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=timestamp,
        ccf_close=ccf_close,
        ccf_close_filled=100.0,
        umc_twd_fair=100.0,
        spread=0.0,
    )


def test_friday_night_is_close_only_under_flat() -> None:
    friday_night = datetime.fromisoformat("2026-06-12T17:25:00+08:00")
    bars = TradingCalendar("flat").annotate([make_bar(0, friday_night)])

    assert bars[0].close_allowed
    assert not bars[0].entry_allowed
    assert bars[0].friday_night_close_only


def test_weekend_session_is_close_only_and_marks_force_close_under_flat() -> None:
    bars = TradingCalendar("flat").annotate(
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


# The CCF/UMC default. Both venues close over the weekend, so neither rule
# applies; measured +19.7% net at an identical max drawdown across three
# sampling frequencies. See docs/CCF_UMC_PLAN.md.
def test_default_weekend_policy_none_drops_both_rules() -> None:
    bars = TradingCalendar().annotate(
        [
            make_bar(0, datetime.fromisoformat("2026-06-12T13:43:00+08:00")),
            make_bar(1, datetime.fromisoformat("2026-06-12T17:25:00+08:00")),
            make_bar(2, datetime.fromisoformat("2026-06-12T17:26:00+08:00")),
            make_bar(3, datetime.fromisoformat("2026-06-15T08:45:00+08:00")),
        ]
    )

    assert bars[1].close_allowed
    assert bars[1].entry_allowed
    assert not bars[1].friday_night_close_only
    assert not bars[1].weekend_session_close_only
    assert not bars[2].friday_session_end_force_close
    assert bars[3].entry_allowed


def test_weekend_policy_no_entry_keeps_the_ban_and_drops_the_force_close() -> None:
    # The Monday bar is load-bearing: a session is only the week's last one if a
    # later close-allowed bar falls in a new ISO week.
    bars = TradingCalendar("no-entry").annotate(
        [
            make_bar(0, datetime.fromisoformat("2026-06-12T13:43:00+08:00")),
            make_bar(1, datetime.fromisoformat("2026-06-12T17:25:00+08:00")),
            make_bar(2, datetime.fromisoformat("2026-06-12T17:26:00+08:00")),
            make_bar(3, datetime.fromisoformat("2026-06-15T08:45:00+08:00")),
        ]
    )

    assert not bars[1].entry_allowed
    assert bars[1].weekend_session_close_only
    assert not bars[2].friday_session_end_force_close
    assert bars[3].entry_allowed


def test_unknown_weekend_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="weekend_policy must be one of"):
        TradingCalendar("flatten")


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


def test_taifex_calendar_weekday_sessions_and_friday_close_only() -> None:
    # taifex_session_status, not live_session_status: this asserts TAIFEX's own
    # hours. live_session_status is the PAIR's session -- TAIFEX intersected
    # with NYSE RTH -- and none of these three instants is inside it.
    weekday_day = taifex_session_status(
        datetime.fromisoformat("2026-06-18T08:45:00+08:00"),
        (),
    )
    weekday_night = taifex_session_status(
        datetime.fromisoformat("2026-06-18T17:25:00+08:00"),
        (),
    )
    friday_night = taifex_session_status(
        datetime.fromisoformat("2026-06-12T17:25:00+08:00"),
        (),
    )

    assert weekday_day.is_trading
    assert not weekday_day.is_close_only
    assert weekday_night.is_trading
    assert not weekday_night.is_close_only
    assert friday_night.is_trading
    assert friday_night.is_close_only


def test_pair_session_is_taifex_intersected_with_nyse_rth() -> None:
    """CCF/UMC trades only where both venues are open at once.

    UMC is a cash equity that prices during RTH alone, so outside it a spread
    built from a frozen UMC price is fiction. The intersection is Taipei
    21:30-04:00 in US summer, which means the pair never trades the TAIFEX DAY
    session at all.
    """
    # TAIFEX day session: open for TAIFEX, but NYSE has been shut for hours.
    day = live_session_status(datetime.fromisoformat("2026-06-18T08:45:00+08:00"), ())
    assert not day.is_trading
    assert day.reason == "us_rth_closed"

    # TAIFEX night session opens at 17:25; NYSE does not open until 21:30.
    early_night = live_session_status(
        datetime.fromisoformat("2026-06-18T17:25:00+08:00"), ()
    )
    assert not early_night.is_trading
    assert early_night.reason == "us_rth_closed"
    assert early_night.next_open_at == datetime.fromisoformat(
        "2026-06-18T21:30:00+08:00"
    )

    # Both open.
    assert live_session_status(
        datetime.fromisoformat("2026-06-18T21:30:00+08:00"), ()
    ).is_trading
    assert live_session_status(
        datetime.fromisoformat("2026-06-19T03:59:00+08:00"), ()
    ).is_trading
    # RTH close is exclusive.
    assert not live_session_status(
        datetime.fromisoformat("2026-06-19T04:00:00+08:00"), ()
    ).is_trading


def test_pair_session_shifts_with_us_dst() -> None:
    """In US winter the window is 22:30-05:00 Taipei, an hour later throughout.

    05:00 is also when the TAIFEX night session ends, so in winter the two
    closes coincide exactly rather than one clipping the other.
    """
    assert not live_session_status(
        datetime.fromisoformat("2026-01-20T21:30:00+08:00"), ()
    ).is_trading
    assert live_session_status(
        datetime.fromisoformat("2026-01-20T22:30:00+08:00"), ()
    ).is_trading
    assert live_session_status(
        datetime.fromisoformat("2026-01-21T04:59:00+08:00"), ()
    ).is_trading
    assert not live_session_status(
        datetime.fromisoformat("2026-01-21T05:00:00+08:00"), ()
    ).is_trading


def test_pair_is_closed_when_taifex_is_closed_even_inside_rth() -> None:
    """A TAIFEX holiday closes the pair regardless of what NYSE is doing."""
    closed = (date(2026, 6, 18),)
    status = live_session_status(
        datetime.fromisoformat("2026-06-18T22:00:00+08:00"), closed
    )

    assert not status.is_trading
    assert status.reason == "closed_date"


def test_inactive_session_is_not_allowed_without_ccf_trades() -> None:
    timestamp = datetime.fromisoformat("2026-06-13T08:45:00+08:00")
    bars = TradingCalendar().annotate([make_bar(0, timestamp, ccf_close=None)])

    assert not bars[0].close_allowed
    assert not bars[0].entry_allowed


def test_weekend_force_exit_fires_in_grace_window_at_the_pair_session_end() -> None:
    # The pair's last session before the weekend is Friday 2026-06-19's US
    # session, closing at Taipei 2026-06-20 04:00 -- an hour BEFORE the TAIFEX
    # night session it sits inside. Anchoring the window to TAIFEX's 05:00 would
    # put it entirely in minutes the loop never processes.
    assert is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T03:57:00+08:00"),
        weekend_policy="flat",
    )
    # Exactly grace_minutes (5) before the close still counts; one minute more
    # does not.
    assert is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T03:55:00+08:00"),
        weekend_policy="flat",
    )
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T03:54:00+08:00"),
        weekend_policy="flat",
    )


def test_weekend_force_exit_does_not_fire_in_taifexs_own_closing_window() -> None:
    """The regression the pair-session anchor exists to prevent.

    04:55-05:00 is the TAIFEX night session's tail and used to be the grace
    window. The pair stopped trading at 04:00, so no bar exists here -- a rule
    that could only fire in this window would be wired up and permanently dead.
    """
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T04:57:00+08:00"),
        weekend_policy="flat",
    )


def test_weekend_force_exit_never_fires_under_the_default_policy() -> None:
    # Same bar as the firing one above, default policy: no force-exit at all.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T03:57:00+08:00")
    )
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T03:57:00+08:00"),
        weekend_policy="no-entry",
    )


def test_weekend_force_exit_ignores_the_start_of_the_pair_session() -> None:
    # Friday's US open, far from its close, must not force-exit.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-19T21:35:00+08:00"),
        weekend_policy="flat",
    )
    # TAIFEX day session: outside the pair's window entirely.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-19T13:42:00+08:00"),
        weekend_policy="flat",
    )


def test_weekend_force_exit_ignores_an_ordinary_weeknight_session_end() -> None:
    # Wednesday's US session closes Thursday 04:00 Taipei, and Thursday's own
    # session follows in the same ISO week, so this is not a weekend break.
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-18T03:57:00+08:00"),
        weekend_policy="flat",
    )


def test_weekend_force_exit_covers_monday_holiday_long_weekend() -> None:
    # 2026-06-22 (Mon) closed: the next session is Tuesday, still a new ISO
    # week, so the Friday flatten must still fire.
    assert is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T03:57:00+08:00"),
        (date(2026, 6, 22),),
        weekend_policy="flat",
    )


def test_weekend_force_exit_is_false_outside_trading_hours() -> None:
    assert not is_weekend_force_exit_bar(
        datetime.fromisoformat("2026-06-20T12:00:00+08:00")
    )


def test_position_sizing_direction_signs(strategy_config, fee_config) -> None:
    short_umc = size_position_for_direction(
        Direction.SHORT_UMC_LONG_CCF,
        umc_price=2500.0,
        ccf_price=250.0,
        strategy=strategy_config,
        fees=fee_config,
    )
    long_umc = size_position_for_direction(
        Direction.LONG_UMC_SHORT_CCF,
        umc_price=2500.0,
        ccf_price=250.0,
        strategy=strategy_config,
        fees=fee_config,
    )

    assert short_umc is not None
    assert long_umc is not None
    assert short_umc.ccf_contracts == 40
    assert short_umc.umc_units == pytest.approx(-80.0)
    assert short_umc.umc_units < 0
    assert short_umc.ccf_units > 0
    assert long_umc.umc_units == pytest.approx(80.0)
    assert long_umc.umc_units > 0
    assert long_umc.ccf_units < 0


def test_position_sizing_uses_umc_contract_quantity(strategy_config, fee_config) -> None:
    sizing = size_position_for_direction(
        Direction.SHORT_UMC_LONG_CCF,
        umc_price=2880.31068,
        ccf_price=2487.5,
        strategy=replace_strategy_notional(strategy_config, 240_000.0),
        fees=fee_config,
    )

    assert sizing is not None
    assert sizing.ccf_contracts == 1
    assert sizing.actual_leg_notional_twd == pytest.approx(248_750.0)
    assert sizing.umc_units == pytest.approx(-17.27244229)


def test_position_sizing_can_use_fixed_ccf_lots(strategy_config, fee_config) -> None:
    sizing = size_position_for_direction(
        Direction.SHORT_UMC_LONG_CCF,
        umc_price=2880.31068,
        ccf_price=2487.5,
        strategy=replace_strategy_ccf_lots(strategy_config, 2),
        fees=fee_config,
    )

    assert sizing is not None
    assert sizing.ccf_contracts == 2
    assert sizing.raw_ccf_contracts == 2.0
    assert sizing.actual_leg_notional_twd == pytest.approx(497_500.0)
    assert sizing.umc_units == pytest.approx(-34.54488459)


def test_fixed_ccf_lots_preserves_direction_signs(strategy_config, fee_config) -> None:
    sizing = size_position_for_direction(
        Direction.LONG_UMC_SHORT_CCF,
        umc_price=2500.0,
        ccf_price=250.0,
        strategy=replace_strategy_ccf_lots(strategy_config, 3),
        fees=fee_config,
    )

    assert sizing is not None
    assert sizing.ccf_contracts == -3
    assert sizing.ccf_units == -300.0
    assert sizing.umc_units == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# CCF's contract size: the highest-consequence constant in the system.
# CCF is 2,000 shares per contract; QFF, which this codebase used to trade, was
# 100. A wrong value sizes every position twenty times off while every downstream
# number stays internally consistent, so nothing else can catch it.
# ---------------------------------------------------------------------------


def ccf_fees(multiplier: float) -> FeeConfig:
    return FeeConfig(
        umc_fee_bps=2.5,
        ccf_fee_per_contract_twd=88.0,
        ccf_tax_rate=0.00002,
        ccf_contract_multiplier=multiplier,
        umc_contract_multiplier=5.0,
    )


def test_one_ccf_lot_is_two_thousand_shares_not_one_hundred() -> None:
    # A realistic CCF price: the measured median is 156 TWD.
    sizing = size_position_for_direction(
        Direction.SHORT_UMC_LONG_CCF,
        umc_price=790.0,
        ccf_price=156.0,
        strategy=StrategyConfig(
            entry_z=1.5,
            exit_z=0.0,
            leg_notional_twd=0.0,
            initial_capital_twd=2_000_000.0,
            max_entry_delay_minutes=15,
            zscore_window=2500,
            ccf_lots=1,
        ),
        fees=ccf_fees(2000.0),
    )

    assert sizing is not None
    assert sizing.ccf_contracts == 1
    assert sizing.ccf_units == 2000.0
    # One contract is ~312,000 TWD, 2.8x a QFF contract -- not ~15,600.
    assert sizing.actual_leg_notional_twd == pytest.approx(312_000.0)


def test_ccf_multiplier_scales_the_umc_leg_by_exactly_twenty() -> None:
    def umc_units_for(multiplier: float) -> float:
        sizing = size_position_for_direction(
            Direction.SHORT_UMC_LONG_CCF,
            umc_price=790.0,
            ccf_price=156.0,
            strategy=StrategyConfig(
                entry_z=1.5,
                exit_z=0.0,
                leg_notional_twd=0.0,
                initial_capital_twd=2_000_000.0,
                max_entry_delay_minutes=15,
                zscore_window=2500,
                ccf_lots=1,
            ),
            fees=ccf_fees(multiplier),
        )
        assert sizing is not None
        return sizing.umc_units

    # Getting the multiplier wrong does not fail anywhere -- it silently hedges
    # the CCF leg with a UMC leg twenty times the wrong size.
    assert umc_units_for(2000.0) == pytest.approx(20.0 * umc_units_for(100.0))


def test_config_refuses_to_guess_the_ccf_contract_multiplier(tmp_path) -> None:
    config_path = tmp_path / "no_multiplier.toml"
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                "input_csv = ''",
                f"store_path = '{(tmp_path / 'store.sqlite3').as_posix()}'",
                "",
                "[fees]",
                "ccf_fee_per_contract_twd = 88.0",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="ccf_contract_multiplier is required"):
        load_config(config_path)


def test_umc_fee_uses_umc_contract_twd_price(fee_config) -> None:
    costs = fill_costs(
        umc_units=-17.27244229,
        umc_price=2880.31068,
        ccf_contracts=1,
        ccf_price=2487.5,
        fees=fee_config,
    )

    assert costs["umc_fee_twd"] == pytest.approx(124.375)


def replace_strategy_notional(strategy_config, leg_notional_twd: float):
    return replace(
        strategy_config,
        leg_notional_twd=leg_notional_twd,
        sizing_mode="notional",
    )


def replace_strategy_ccf_lots(strategy_config, ccf_lots: int):
    return replace(strategy_config, ccf_lots=ccf_lots, sizing_mode="fixed_lots")
