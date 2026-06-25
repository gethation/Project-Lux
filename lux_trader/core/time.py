from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def ensure_taipei(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=TAIPEI_TZ)
    return timestamp.astimezone(TAIPEI_TZ)

