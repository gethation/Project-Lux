"""NYSE regular trading hours, expressed in Taipei time.

Pure zoneinfo arithmetic with no venue dependency, so the pair's session can be
reasoned about and tested without a Gateway anywhere near it.

US DST is why this is not two constants. A UMC session for market date D runs
Taipei D 21:30 -> D+1 04:00 in US summer and 22:30 -> D+1 05:00 in winter, so
the whole window shifts by an hour twice a year -- and in winter its tail lands
exactly on the TAIFEX night session's 05:00 close.

US market holidays are NOT modelled. On one, the pair looks open, the UMC feed
serves nothing, the staleness gate skips every minute and no bar is built. That
is loud and safe rather than silent and wrong, and core.calendar names the
reason in the session status so it does not read as a data fault.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .time import TAIPEI_TZ


UMC_RTH_OPEN = time(9, 30)
UMC_RTH_CLOSE = time(16, 0)
US_MARKET_TIME_ZONE = "US/Eastern"


@dataclass(frozen=True)
class UmcRthSession:
    market_date: date
    opens_at: datetime
    closes_at: datetime


def umc_rth_session(
    market_date: date,
    *,
    market_time_zone_id: str = US_MARKET_TIME_ZONE,
) -> UmcRthSession:
    """Convert UMC's fixed Eastern RTH clock to Taipei with zoneinfo DST rules."""

    eastern = ZoneInfo(market_time_zone_id)
    opens_eastern = datetime.combine(market_date, UMC_RTH_OPEN, tzinfo=eastern)
    closes_eastern = datetime.combine(market_date, UMC_RTH_CLOSE, tzinfo=eastern)
    return UmcRthSession(
        market_date=market_date,
        opens_at=opens_eastern.astimezone(TAIPEI_TZ),
        closes_at=closes_eastern.astimezone(TAIPEI_TZ),
    )


def umc_rth_status(observed_at: datetime) -> tuple[bool, datetime]:
    """``(inside RTH, next RTH open)`` for a Taipei instant.

    A session for US market date D can span two Taipei dates, so the session
    containing ``observed_at`` may belong to yesterday's US date. Both
    candidates are checked.
    """
    for offset in (1, 0):
        market_date = observed_at.date() - timedelta(days=offset)
        if market_date.weekday() >= 5:
            continue
        session = umc_rth_session(market_date)
        if session.opens_at <= observed_at < session.closes_at:
            return True, session.opens_at

    for offset in range(0, 9):
        market_date = observed_at.date() + timedelta(days=offset - 1)
        if market_date.weekday() >= 5:
            continue
        session = umc_rth_session(market_date)
        if session.opens_at > observed_at:
            return False, session.opens_at
    raise RuntimeError(f"No UMC RTH open found within a week of {observed_at}")


def umc_rth_close_for(observed_at: datetime) -> datetime | None:
    """Close of the RTH session containing ``observed_at``, or None if outside."""
    for offset in (1, 0):
        market_date = observed_at.date() - timedelta(days=offset)
        if market_date.weekday() >= 5:
            continue
        session = umc_rth_session(market_date)
        if session.opens_at <= observed_at < session.closes_at:
            return session.closes_at
    return None


def filter_umc_rth_minutes(index):
    """Keep only the minutes of a Taipei DatetimeIndex that fall inside UMC RTH.

    Used to build the warmup index: the TAIFEX session index is the superset and
    the pair's own minutes are its intersection with NYSE RTH. Sessions are
    cached per US market date because a 14-day window is ~16k minutes while each
    date's session is a fixed pair of instants.
    """
    sessions: dict[date, UmcRthSession] = {}

    def contains(moment: datetime) -> bool:
        for offset in (1, 0):
            market_date = moment.date() - timedelta(days=offset)
            if market_date.weekday() >= 5:
                continue
            session = sessions.get(market_date)
            if session is None:
                session = umc_rth_session(market_date)
                sessions[market_date] = session
            if session.opens_at <= moment < session.closes_at:
                return True
        return False

    return index[[contains(ts.to_pydatetime()) for ts in index]]


__all__ = [
    "UMC_RTH_CLOSE",
    "UMC_RTH_OPEN",
    "US_MARKET_TIME_ZONE",
    "UmcRthSession",
    "filter_umc_rth_minutes",
    "umc_rth_close_for",
    "umc_rth_session",
    "umc_rth_status",
]
