from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from datetime import datetime, time, timedelta, tzinfo

from .models import MarketBar
from .us_calendar import umc_rth_close_for, umc_rth_status


DAY_START = 8 * 60 + 45
DAY_END = 13 * 60 + 45
NIGHT_START = 17 * 60 + 25
NIGHT_END = 5 * 60

# How many minutes before a session's nominal end the live loop is still allowed
# to fire a weekend force-exit. The live minute-bar builder finalizes minute M
# only when minute M+1 opens, so it never processes the session's final minute;
# a few minutes of grace guarantees at least one processed bar triggers the exit,
# and tolerates a short data gap at the very end of the session.
WEEKEND_FORCE_EXIT_GRACE_MINUTES = 5

# Which of the two weekend rules applies to the last trading session of each ISO
# week. Same vocabulary as the PoC's --weekend-policy so backtest and live agree.
#
#   flat      no entries in that session, and force-close on its final bar
#   no-entry  keep the entry ban, drop the force-close
#   none      neither rule
#
# CCF/UMC runs 'none'. The rule was inherited from QFF/TSM, where Binance's
# perpetual traded through the weekend while TAIFEX froze, leaving one leg
# uncovered. TAIFEX and NYSE both close, so there is no such exposure here, and
# removing it measured +19.7% net with an IDENTICAL max drawdown -- consistently
# across 1m, 5m and 15m sampling, three independent datasets. Removing it is
# deleting a mis-transplanted rule, not loosening risk control.
#
# 'flat' survives only to keep the legacy QFF/TSM replay golden reproducible as
# a refactor tripwire. Phase C deletes it along with that fixture.
WEEKEND_POLICY_FLAT = "flat"
WEEKEND_POLICY_NO_ENTRY = "no-entry"
WEEKEND_POLICY_NONE = "none"
WEEKEND_POLICIES = (
    WEEKEND_POLICY_FLAT,
    WEEKEND_POLICY_NO_ENTRY,
    WEEKEND_POLICY_NONE,
)
DEFAULT_WEEKEND_POLICY = WEEKEND_POLICY_NONE


def validate_weekend_policy(value: str, *, field: str = "weekend_policy") -> str:
    policy = str(value).strip().lower()
    if policy not in WEEKEND_POLICIES:
        allowed = ", ".join(repr(name) for name in WEEKEND_POLICIES)
        raise ValueError(f"{field} must be one of {allowed}; got {value!r}")
    return policy


def weekend_force_close_enabled(policy: str) -> bool:
    return policy == WEEKEND_POLICY_FLAT


def weekend_entry_ban_enabled(policy: str) -> bool:
    return policy in {WEEKEND_POLICY_FLAT, WEEKEND_POLICY_NO_ENTRY}


@dataclass(frozen=True)
class LiveSessionStatus:
    is_trading: bool
    is_close_only: bool
    reason: str
    next_open_at: datetime
    countdown: timedelta


def minute_of_day(timestamp: datetime) -> int:
    return timestamp.hour * 60 + timestamp.minute


def session_start_date(timestamp: datetime) -> datetime.date:
    if minute_of_day(timestamp) <= NIGHT_END:
        return (timestamp - timedelta(days=1)).date()
    return timestamp.date()


def in_day_session(timestamp: datetime) -> bool:
    minute = minute_of_day(timestamp)
    return DAY_START <= minute <= DAY_END


def in_night_session(timestamp: datetime) -> bool:
    minute = minute_of_day(timestamp)
    return minute >= NIGHT_START or minute <= NIGHT_END


def is_live_business_day(value: date, closed_dates: Iterable[date]) -> bool:
    return value.weekday() < 5 and value not in set(closed_dates)


def taifex_session_status(
    timestamp: datetime,
    closed_dates: Iterable[date] = (),
) -> LiveSessionStatus:
    """TAIFEX's own session, before intersecting with the US leg."""
    closed = set(closed_dates)
    trading = _is_live_session_trading(timestamp, closed)
    session_start = session_start_date(timestamp)
    close_only = (
        trading
        and in_night_session(timestamp)
        and session_start.weekday() == 4
    )
    next_open = next_trading_session_start(timestamp, closed)
    if trading:
        reason = "close_only" if close_only else "open"
    elif timestamp.date() in closed or session_start in closed:
        reason = "closed_date"
    elif timestamp.date().weekday() >= 5 or session_start.weekday() >= 5:
        reason = "weekend"
    else:
        reason = "outside_session"
    return LiveSessionStatus(
        is_trading=trading,
        is_close_only=close_only,
        reason=reason,
        next_open_at=next_open,
        countdown=max(next_open - timestamp, timedelta(0)),
    )


def live_session_status(
    timestamp: datetime,
    closed_dates: Iterable[date] = (),
) -> LiveSessionStatus:
    """The CCF/UMC pair's session: TAIFEX open AND NYSE RTH open.

    Both legs have to be priceable at once. UMC is a cash equity that only
    prices during RTH, so outside it a spread built from a frozen UMC price is
    fiction -- the loop should idle rather than spray staleness warnings for
    hours. That intersection is Taipei 21:30-04:00 in US summer and 22:30-05:00
    in winter, which means this pair never trades the TAIFEX DAY session at all.

    Winter's tail is the interesting edge: RTH closes at Taipei 05:00, exactly
    when the TAIFEX night session does, so the two boundaries coincide instead
    of one clipping the other.
    """
    base = taifex_session_status(timestamp, closed_dates)
    inside_rth, next_rth_open = umc_rth_status(timestamp)

    if base.is_trading and inside_rth:
        return base
    if base.is_trading:
        return LiveSessionStatus(
            is_trading=False,
            is_close_only=False,
            reason="us_rth_closed",
            # Display-only approximation: TAIFEX could close again inside a long
            # US break. This value feeds the countdown and the non-trading
            # event, never a trading decision.
            next_open_at=max(next_rth_open, timestamp),
            countdown=max(next_rth_open - timestamp, timedelta(0)),
        )
    # TAIFEX is closed. If UMC is closed too, the pair reopens at whichever
    # market opens later; if UMC is somehow open, TAIFEX's own next open governs.
    if not inside_rth and next_rth_open > base.next_open_at:
        return LiveSessionStatus(
            is_trading=False,
            is_close_only=False,
            reason=base.reason,
            next_open_at=next_rth_open,
            countdown=max(next_rth_open - timestamp, timedelta(0)),
        )
    return base


def next_trading_session_start(
    timestamp: datetime,
    closed_dates: Iterable[date] = (),
) -> datetime:
    closed = set(closed_dates)
    tzinfo = timestamp.tzinfo
    start_date = timestamp.date() - timedelta(days=1)
    candidates: list[datetime] = []
    for offset in range(16):
        current = start_date + timedelta(days=offset)
        if not is_live_business_day(current, closed):
            continue
        for session_time in (market_time(8, 45), market_time(17, 25)):
            candidate = datetime.combine(current, session_time, tzinfo=tzinfo)
            if candidate > timestamp:
                candidates.append(candidate)
    if not candidates:
        raise RuntimeError("Unable to find next trading session within 15 days")
    return min(candidates)


def _is_live_session_trading(timestamp: datetime, closed_dates: set[date]) -> bool:
    if timestamp.date() in closed_dates:
        return False
    if in_day_session(timestamp):
        return is_live_business_day(timestamp.date(), closed_dates)
    if in_night_session(timestamp):
        return is_live_business_day(session_start_date(timestamp), closed_dates)
    return False


def _at_minute_of_day(day: date, minute: int, tz: tzinfo | None) -> datetime:
    return datetime.combine(
        day, time(hour=minute // 60, minute=minute % 60), tzinfo=tz
    )


def session_end_minute(timestamp: datetime) -> datetime | None:
    """Nominal last minute of the trading session that contains ``timestamp``.

    Day sessions end at 13:45 the same day; night sessions end at 05:00 the next
    day. Returns None when the timestamp is not on a session clock.
    """
    minute = minute_of_day(timestamp)
    if DAY_START <= minute <= DAY_END:
        return _at_minute_of_day(timestamp.date(), DAY_END, timestamp.tzinfo)
    if minute >= NIGHT_START:
        return _at_minute_of_day(
            timestamp.date() + timedelta(days=1), NIGHT_END, timestamp.tzinfo
        )
    if minute <= NIGHT_END:
        return _at_minute_of_day(timestamp.date(), NIGHT_END, timestamp.tzinfo)
    return None


def is_weekend_force_exit_bar(
    timestamp: datetime,
    closed_dates: Iterable[date] = (),
    *,
    grace_minutes: int = WEEKEND_FORCE_EXIT_GRACE_MINUTES,
    weekend_policy: str = DEFAULT_WEEKEND_POLICY,
) -> bool:
    """Live equivalent of the PoC ``friday_session_end_force_close`` mask.

    True when ``timestamp`` is a trading minute within ``grace_minutes`` of the end
    of the last trading session before a market break that crosses into a new ISO
    week (a weekend, or a weekend extended by a Monday/holiday).

    INHERITED FROM QFF/TSM, AND MEASURED WRONG FOR CCF/UMC. The rule exists
    because Binance's TSM perpetual traded 24/7 while TAIFEX froze, so holding
    over a weekend left one leg uncovered. CCF/UMC has no such structure --
    TAIFEX and NYSE both close -- and removing the rule measured +19.7% net with
    an IDENTICAL max drawdown, consistently across 1m, 5m and 15m sampling.
    Phase A3 turns it into a policy switch defaulting to 'none'; it stays wired
    only so the QFF/TSM replay golden keeps working as a tripwire until the
    CCF/UMC golden replaces it in Phase C. See docs/CCF_UMC_PLAN.md.

    The grace window is measured against the PAIR's session close, not TAIFEX's.
    TAIFEX's night session runs an hour past the pair's, so a window anchored to
    it would sit entirely in minutes the loop never processes -- the rule would
    be wired up and silently unable to fire. Same reasoning as the rollover
    deadline in core/contract_policy.

    Known limitation: a holiday on the *Friday* itself is not covered, because
    the calendar treats a closed date's early-morning hours as non-trading, so
    the preceding session's tail is truncated at midnight and the grace window
    never lands on a processed bar.
    """
    if not weekend_force_close_enabled(validate_weekend_policy(weekend_policy)):
        return False
    closed = set(closed_dates)
    if not live_session_status(timestamp, closed).is_trading:
        return False
    end = umc_rth_close_for(timestamp)
    if end is None:
        return False
    seconds_to_end = (end - timestamp).total_seconds()
    if seconds_to_end < 0 or seconds_to_end > grace_minutes * 60:
        return False
    _, next_start = umc_rth_status(end)
    current_iso = timestamp.isocalendar()
    next_iso = next_start.isocalendar()
    return (current_iso[0], current_iso[1]) != (next_iso[0], next_iso[1])


class TradingCalendar:
    """TAIFEX replay calendar that mirrors the PoC active-session masks."""

    def __init__(self, weekend_policy: str = DEFAULT_WEEKEND_POLICY) -> None:
        self.weekend_policy = validate_weekend_policy(weekend_policy)

    def annotate(self, bars: Iterable[MarketBar]) -> list[MarketBar]:
        rows = list(bars)
        day_active: set[datetime.date] = set()
        night_active: set[datetime.date] = set()

        for bar in rows:
            if bar.ccf_close is None:
                continue
            if in_day_session(bar.timestamp):
                day_active.add(bar.timestamp.date())
            if in_night_session(bar.timestamp):
                night_active.add(session_start_date(bar.timestamp))

        raw_masks: list[tuple[bool, bool, bool, str]] = []
        for bar in rows:
            day_allowed = (
                in_day_session(bar.timestamp) and bar.timestamp.date() in day_active
            )
            session_start = session_start_date(bar.timestamp)
            night_allowed = in_night_session(bar.timestamp) and session_start in night_active
            close_allowed = day_allowed or night_allowed
            friday_night = night_allowed and session_start.weekday() == 4
            session_kind = "N" if in_night_session(bar.timestamp) else "D"
            session_key = f"{session_kind}:{session_start.isoformat()}"
            raw_masks.append((close_allowed, friday_night, False, session_key))

        # Both rules key off the same week-end detection, so it is computed once
        # and gated separately: 'no-entry' still needs the session set even
        # though it drops the force-close.
        week_end_bars = compute_week_end_force_close(rows, raw_masks)
        week_end_sessions = {
            raw_masks[index][3] for index, marked in enumerate(week_end_bars) if marked
        }
        apply_force_close = weekend_force_close_enabled(self.weekend_policy)
        apply_entry_ban = weekend_entry_ban_enabled(self.weekend_policy)

        annotated: list[MarketBar] = []
        for index, bar in enumerate(rows):
            close_allowed, friday_night, _, session_key = raw_masks[index]
            weekend_close_only = (
                apply_entry_ban and close_allowed and session_key in week_end_sessions
            )
            friday_night_close_only = apply_entry_ban and close_allowed and friday_night
            close_only = friday_night_close_only or weekend_close_only
            annotated.append(
                replace(
                    bar,
                    close_allowed=close_allowed,
                    entry_allowed=close_allowed and not close_only,
                    friday_night_close_only=friday_night_close_only,
                    weekend_session_close_only=weekend_close_only,
                    friday_session_end_force_close=(
                        week_end_bars[index] if apply_force_close else False
                    ),
                )
            )
        return annotated


def compute_week_end_force_close(
    rows: list[MarketBar],
    raw_masks: list[tuple[bool, bool, bool, str]],
) -> list[bool]:
    force_close = [False] * len(rows)
    close_indices = [
        index for index, (close_allowed, _, _, _) in enumerate(raw_masks) if close_allowed
    ]
    for current_idx, next_idx in zip(close_indices[:-1], close_indices[1:]):
        current_iso = rows[current_idx].timestamp.isocalendar()
        next_iso = rows[next_idx].timestamp.isocalendar()
        if (current_iso.year, current_iso.week) != (next_iso.year, next_iso.week):
            force_close[current_idx] = True
    return force_close


def is_close_only(timestamp: datetime, close_allowed: bool) -> bool:
    return close_allowed and in_night_session(timestamp) and session_start_date(timestamp).weekday() == 4


def annotate_live_bar(bar: MarketBar) -> MarketBar:
    return annotate_live_bar_with_closed_dates(bar, ())


def annotate_live_bar_with_closed_dates(
    bar: MarketBar,
    closed_dates: Iterable[date],
    *,
    weekend_policy: str = DEFAULT_WEEKEND_POLICY,
) -> MarketBar:
    status = live_session_status(bar.timestamp, closed_dates)
    close_only = status.is_close_only and weekend_entry_ban_enabled(
        validate_weekend_policy(weekend_policy)
    )
    return replace(
        bar,
        close_allowed=status.is_trading,
        entry_allowed=status.is_trading and not close_only,
        friday_night_close_only=status.is_trading and close_only,
        weekend_session_close_only=False,
        friday_session_end_force_close=False,
    )


def market_time(hour: int, minute: int) -> time:
    return time(hour=hour, minute=minute)
