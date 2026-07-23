"""Per-pair weekend policy (§5.3 of docs/MULTIPAIR_PLAN.md).

QFF/TSM keeps both weekend rules because Binance perpetuals trade through the
weekend while TAIFEX is frozen, leaving an open position half-hedged. CCF/UMC has
no such structure -- TAIFEX and NYSE both close -- so it drops them, which the PoC
measured as +19.7% net with an unchanged max drawdown.

The three policies mirror the PoC's ``--weekend-policy`` exactly:

    flat      entry ban on the week's last session + force-close on its final bar
    no-entry  entry ban only
    none      neither
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lux_trader.core.calendar import (
    TaifexSessionCalendar,
    annotate_live_bar_with_closed_dates,
    is_weekend_force_exit_bar,
    validate_weekend_policy,
    weekend_entry_ban_enabled,
    weekend_force_close_enabled,
)
from lux_trader.core.models import MarketBar


# 2026-06-19 is a Friday. Its night session runs to Saturday 05:00, after which
# TAIFEX is frozen until Monday -- the week-end boundary both rules key off.
FRIDAY_SESSION_END = datetime.fromisoformat("2026-06-20T04:57:00+08:00")


def make_bar(index: int, timestamp: datetime) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=timestamp,
        tw_leg_close=100.0,
        tw_leg_close_filled=100.0,
        us_leg_twd_fair=100.0,
        spread=0.0,
    )


def minute_range(start: str, minutes: int) -> list[MarketBar]:
    begin = datetime.fromisoformat(start)
    return [make_bar(i, begin + timedelta(minutes=i)) for i in range(minutes)]


def week_spanning_bars() -> list[MarketBar]:
    """Friday night through the following Monday day session."""
    bars: list[MarketBar] = []
    bars += minute_range("2026-06-19T17:25:00+08:00", 60)  # Fri night open
    bars += minute_range("2026-06-20T04:00:00+08:00", 61)  # Sat 04:00-05:00 close
    bars += minute_range("2026-06-22T08:45:00+08:00", 60)  # Mon day open
    for index, bar in enumerate(bars):
        bars[index] = bar.__class__(**{**bar.__dict__, "row_index": index})
    return bars


@pytest.mark.parametrize(
    ("policy", "force_close", "entry_ban"),
    (
        ("flat", True, True),
        ("no-entry", False, True),
        ("none", False, False),
    ),
)
def test_policy_predicates(policy: str, force_close: bool, entry_ban: bool) -> None:
    assert weekend_force_close_enabled(policy) is force_close
    assert weekend_entry_ban_enabled(policy) is entry_ban


@pytest.mark.parametrize("value", ("flat", "no-entry", "none", "FLAT", "  none  "))
def test_validate_accepts_the_three_policies(value: str) -> None:
    assert validate_weekend_policy(value) in {"flat", "no-entry", "none"}


@pytest.mark.parametrize("value", ("", "off", "no_entry", "noentry", "true"))
def test_validate_rejects_anything_else(value: str) -> None:
    with pytest.raises(ValueError, match="weekend_policy"):
        validate_weekend_policy(value)


@pytest.mark.parametrize(
    ("policy", "expected"),
    (("flat", True), ("no-entry", False), ("none", False)),
)
def test_live_force_exit_only_fires_under_flat(policy: str, expected: bool) -> None:
    assert (
        is_weekend_force_exit_bar(FRIDAY_SESSION_END, weekend_policy=policy) is expected
    )


def test_live_force_exit_defaults_to_flat() -> None:
    """An omitted policy must keep the behaviour QFF/TSM runs on today."""
    assert is_weekend_force_exit_bar(FRIDAY_SESSION_END) is True


@pytest.mark.parametrize(
    ("policy", "entry_allowed"),
    (("flat", False), ("no-entry", False), ("none", True)),
)
def test_live_bar_entry_ban_follows_policy(policy: str, entry_allowed: bool) -> None:
    # Friday night is close-only under the entry ban; 'none' reopens it.
    bar = make_bar(0, datetime.fromisoformat("2026-06-19T20:00:00+08:00"))

    annotated = annotate_live_bar_with_closed_dates(bar, (), weekend_policy=policy)

    assert annotated.close_allowed is True
    assert annotated.entry_allowed is entry_allowed
    assert annotated.friday_night_close_only is (not entry_allowed)


def test_replay_calendar_flat_bans_entry_and_force_closes() -> None:
    annotated = TaifexSessionCalendar("flat").annotate(week_spanning_bars())

    friday = [bar for bar in annotated if bar.timestamp.day in {19, 20}]
    assert any(bar.friday_session_end_force_close for bar in friday)
    assert all(not bar.entry_allowed for bar in friday if bar.close_allowed)


def test_replay_calendar_no_entry_keeps_ban_and_drops_force_close() -> None:
    annotated = TaifexSessionCalendar("no-entry").annotate(week_spanning_bars())

    friday = [bar for bar in annotated if bar.timestamp.day in {19, 20}]
    assert not any(bar.friday_session_end_force_close for bar in friday)
    # The entry ban survives even though nothing is force-closed any more -- this
    # is the case that would silently break if the session set were derived from
    # the gated mask instead of the raw one.
    assert all(not bar.entry_allowed for bar in friday if bar.close_allowed)


def test_replay_calendar_none_drops_both_rules() -> None:
    annotated = TaifexSessionCalendar("none").annotate(week_spanning_bars())

    friday = [bar for bar in annotated if bar.timestamp.day in {19, 20}]
    assert not any(bar.friday_session_end_force_close for bar in friday)
    assert all(bar.entry_allowed for bar in friday if bar.close_allowed)
    assert not any(bar.weekend_session_close_only for bar in annotated)
    assert not any(bar.friday_night_close_only for bar in annotated)


def test_replay_calendar_defaults_to_flat() -> None:
    default = TaifexSessionCalendar().annotate(week_spanning_bars())
    explicit = TaifexSessionCalendar("flat").annotate(week_spanning_bars())

    assert default == explicit


def test_monday_session_is_untouched_by_any_policy() -> None:
    """Only the week's last session is affected; the next week opens normally."""
    for policy in ("flat", "no-entry", "none"):
        annotated = TaifexSessionCalendar(policy).annotate(week_spanning_bars())
        monday = [bar for bar in annotated if bar.timestamp.day == 22]
        assert monday, "fixture must include Monday bars"
        assert all(bar.entry_allowed for bar in monday if bar.close_allowed)
        assert not any(bar.friday_session_end_force_close for bar in monday)
