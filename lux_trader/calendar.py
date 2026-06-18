from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, time, timedelta

from .models import MarketBar


DAY_START = 8 * 60 + 45
DAY_END = 13 * 60 + 45
NIGHT_START = 17 * 60 + 25
NIGHT_END = 5 * 60


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


class TradingCalendar:
    """QFF replay calendar that mirrors the PoC active-session masks."""

    def annotate(self, bars: Iterable[MarketBar]) -> list[MarketBar]:
        rows = list(bars)
        day_active: set[datetime.date] = set()
        night_active: set[datetime.date] = set()

        for bar in rows:
            if bar.qff_close is None:
                continue
            if in_day_session(bar.timestamp):
                day_active.add(bar.timestamp.date())
            if in_night_session(bar.timestamp):
                night_active.add(session_start_date(bar.timestamp))

        annotated: list[MarketBar] = []
        for bar in rows:
            day_allowed = (
                in_day_session(bar.timestamp) and bar.timestamp.date() in day_active
            )
            session_start = session_start_date(bar.timestamp)
            night_allowed = in_night_session(bar.timestamp) and session_start in night_active
            close_allowed = day_allowed or night_allowed
            friday_night = night_allowed and session_start.weekday() == 4
            annotated.append(
                replace(
                    bar,
                    close_allowed=close_allowed,
                    entry_allowed=close_allowed and not friday_night,
                    friday_night_close_only=close_allowed and friday_night,
                )
            )
        return annotated


def is_close_only(timestamp: datetime, close_allowed: bool) -> bool:
    return close_allowed and in_night_session(timestamp) and session_start_date(timestamp).weekday() == 4


def annotate_live_bar(bar: MarketBar) -> MarketBar:
    close_allowed = in_day_session(bar.timestamp) or in_night_session(bar.timestamp)
    friday_night = (
        close_allowed
        and in_night_session(bar.timestamp)
        and session_start_date(bar.timestamp).weekday() == 4
    )
    return replace(
        bar,
        close_allowed=close_allowed,
        entry_allowed=close_allowed and not friday_night,
        friday_night_close_only=close_allowed and friday_night,
    )


def market_time(hour: int, minute: int) -> time:
    return time(hour=hour, minute=minute)
