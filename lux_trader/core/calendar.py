from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from datetime import datetime, time, timedelta, tzinfo

from .models import MarketBar


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


def live_session_status(
    timestamp: datetime,
    closed_dates: Iterable[date] = (),
) -> LiveSessionStatus:
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
) -> bool:
    """Live equivalent of the PoC ``friday_session_end_force_close`` mask.

    True when ``timestamp`` is a trading minute within ``grace_minutes`` of the end
    of the last trading session before a market break that crosses into a new ISO
    week (a weekend, or a weekend extended by a Monday/holiday). The live loop uses
    this to flatten an open position before TAIFEX is frozen over the weekend while the
    USD-denominated venue keeps trading — the uncovered-leg gap risk the PoC
    strategy always closes out.

    Known limitation: a holiday on the *Friday* itself is not covered, because the
    live calendar treats the early-morning hours of a closed date as non-trading,
    so the preceding Thursday-night session's tail is truncated at midnight and the
    grace window never lands on a processed bar. See the follow-up note in the
    weekend force-close design.
    """
    closed = set(closed_dates)
    if not _is_live_session_trading(timestamp, closed):
        return False
    end = session_end_minute(timestamp)
    if end is None:
        return False
    seconds_to_end = (end - timestamp).total_seconds()
    if seconds_to_end < 0 or seconds_to_end > grace_minutes * 60:
        return False
    next_start = next_trading_session_start(end, closed)
    current_iso = timestamp.isocalendar()
    next_iso = next_start.isocalendar()
    return (current_iso[0], current_iso[1]) != (next_iso[0], next_iso[1])


class TradingCalendar:
    """TAIFEX replay calendar that mirrors the PoC active-session masks."""

    def annotate(self, bars: Iterable[MarketBar]) -> list[MarketBar]:
        rows = list(bars)
        day_active: set[datetime.date] = set()
        night_active: set[datetime.date] = set()

        for bar in rows:
            if bar.tw_leg_close is None:
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

        force_close = compute_week_end_force_close(rows, raw_masks)
        weekend_close_only_sessions = {
            raw_masks[index][3] for index, marked in enumerate(force_close) if marked
        }

        annotated: list[MarketBar] = []
        for index, bar in enumerate(rows):
            close_allowed, friday_night, _, session_key = raw_masks[index]
            weekend_close_only = close_allowed and session_key in weekend_close_only_sessions
            close_only = friday_night or weekend_close_only
            annotated.append(
                replace(
                    bar,
                    close_allowed=close_allowed,
                    entry_allowed=close_allowed and not close_only,
                    friday_night_close_only=close_allowed and friday_night,
                    weekend_session_close_only=weekend_close_only,
                    friday_session_end_force_close=force_close[index],
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
) -> MarketBar:
    status = live_session_status(bar.timestamp, closed_dates)
    return replace(
        bar,
        close_allowed=status.is_trading,
        entry_allowed=status.is_trading and not status.is_close_only,
        friday_night_close_only=status.is_trading and status.is_close_only,
        weekend_session_close_only=False,
        friday_session_end_force_close=False,
    )


def market_time(hour: int, minute: int) -> time:
    return time(hour=hour, minute=minute)
