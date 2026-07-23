from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from ...core.time import TAIPEI_TZ


UMC_RTH_OPEN = time(9, 30)
UMC_RTH_CLOSE = time(16, 0)


@dataclass(frozen=True)
class UmcRthSession:
    market_date: date
    opens_at: datetime
    closes_at: datetime


def umc_rth_session(
    market_date: date,
    *,
    market_time_zone_id: str = "US/Eastern",
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


__all__ = [
    "UMC_RTH_CLOSE",
    "UMC_RTH_OPEN",
    "UmcRthSession",
    "umc_rth_session",
]
