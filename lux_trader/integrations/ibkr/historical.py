from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

import pandas as pd

from ...core.time import TAIPEI_TZ
from .client_process import IbkrClientProcess


# One complete RTH session is 09:30-16:00 ET, i.e. exactly 390 one-minute bars.
RTH_BARS_PER_SESSION = 390

# A single "2 M" request returns ~15,600 bars (measured 2026-07-24), so even a
# two-year backfill is about a dozen requests.
DEFAULT_CHUNK_DURATION = "2 M"
DEFAULT_CHUNK_DAYS = 60

# IBKR pacing: identical requests must be >15s apart, fewer than 6 requests per
# 2s for one contract, and no more than 60 requests per 10 minutes. The last one
# binds a bulk backfill, so ~11s between requests keeps us inside all three with
# room to spare. Requests here are never identical (endDateTime advances), but
# the conservative spacing costs almost nothing at this volume.
DEFAULT_REQUEST_SPACING_SECONDS = 11.0

BAR_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


@dataclass
class HistoricalFetchReport:
    """What a backfill actually did, including anything that looks wrong."""

    chunks_requested: int = 0
    raw_bars: int = 0
    unique_bars: int = 0
    duplicate_bars: int = 0
    sessions: int = 0
    incomplete_sessions: list[tuple[str, int]] = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None

    def summary_lines(self) -> list[str]:
        lines = [
            f"chunks requested : {self.chunks_requested}",
            f"bars             : {self.unique_bars:,} unique "
            f"({self.duplicate_bars:,} duplicates dropped)",
            f"sessions         : {self.sessions}",
        ]
        if self.start is not None and self.end is not None:
            lines.append(f"range            : {self.start} -> {self.end}")
        if self.incomplete_sessions:
            shown = ", ".join(
                f"{day} ({count})" for day, count in self.incomplete_sessions[:5]
            )
            lines.append(
                f"incomplete       : {len(self.incomplete_sessions)} session(s) "
                f"not {RTH_BARS_PER_SESSION} bars -- {shown}"
            )
        return lines


def _format_end(moment: datetime) -> str:
    """IBKR endDateTime format. UTC is explicit so the Gateway's own timezone
    setting cannot reinterpret it."""
    return moment.astimezone(UTC).strftime("%Y%m%d-%H:%M:%S")


def bars_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Normalize worker rows to Taipei-stamped OHLCV matching the PoC schema."""
    if not rows:
        return pd.DataFrame(columns=BAR_COLUMNS)
    frame = pd.DataFrame(rows)
    timestamps = pd.to_datetime(frame["date"], utc=True)
    frame["timestamp"] = timestamps.dt.tz_convert(TAIPEI_TZ)
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[BAR_COLUMNS].dropna(subset=["open", "close"])
    return frame.sort_values("timestamp").reset_index(drop=True)


def session_days(frame: pd.DataFrame) -> pd.Series:
    """Group each RTH session onto the US market date it belongs to.

    A Taipei-stamped RTH session spans midnight (21:30 one day to 04:00 the next
    in summer), so the calendar date alone would split it in two.
    """
    timestamps = pd.DatetimeIndex(frame["timestamp"])
    return pd.Series((timestamps - pd.Timedelta(hours=12)).date, index=frame.index)


def fetch_umc_1m_history(
    client: IbkrClientProcess,
    *,
    months: int = 3,
    chunk_duration: str = DEFAULT_CHUNK_DURATION,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    request_spacing_seconds: float = DEFAULT_REQUEST_SPACING_SECONDS,
    end: datetime | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    reporter: Callable[[str], None] = print,
) -> tuple[pd.DataFrame, HistoricalFetchReport]:
    """Back-fill UMC 1-minute RTH bars, walking backwards in chunks.

    Returns the merged frame plus a report. The report is deliberately loud about
    incomplete sessions: a trading day that is not exactly 390 bars means data is
    missing, and that should be investigated rather than forward-filled.
    """
    if months <= 0:
        raise ValueError("months must be positive")
    if chunk_days <= 0:
        raise ValueError("chunk_days must be positive")
    if request_spacing_seconds < 0:
        raise ValueError("request_spacing_seconds must not be negative")

    end_moment = (end or datetime.now(UTC)).astimezone(UTC)
    earliest = end_moment - timedelta(days=months * 31)
    report = HistoricalFetchReport()
    collected: list[dict[str, Any]] = []

    cursor = end_moment
    while cursor > earliest:
        if report.chunks_requested and request_spacing_seconds:
            sleeper(request_spacing_seconds)
        reporter(
            f"  requesting {chunk_duration} of 1m TRADES ending "
            f"{cursor.astimezone(TAIPEI_TZ):%Y-%m-%d %H:%M} Taipei"
        )
        rows = client.fetch_umc_historical_1m(
            end_date_time=_format_end(cursor),
            duration=chunk_duration,
            use_rth=True,
        )
        report.chunks_requested += 1
        report.raw_bars += len(rows)
        if not rows:
            reporter("    empty chunk; stopping the walk backwards")
            break
        collected.extend(rows)
        oldest = min(pd.to_datetime(row["date"], utc=True) for row in rows)
        next_cursor = oldest.to_pydatetime() - timedelta(minutes=1)
        if next_cursor >= cursor:
            reporter("    cursor stopped advancing; stopping to avoid a loop")
            break
        cursor = next_cursor

    frame = bars_to_frame(collected)
    report.duplicate_bars = len(frame) - frame["timestamp"].nunique()
    frame = frame.drop_duplicates(subset=["timestamp"], keep="last").reset_index(
        drop=True
    )
    report.unique_bars = len(frame)
    if frame.empty:
        return frame, report

    days = session_days(frame)
    report.sessions = int(days.nunique())
    report.start = frame["timestamp"].iloc[0]
    report.end = frame["timestamp"].iloc[-1]

    counts = frame.groupby(days).size()
    # the first and last sessions are usually clipped by the requested window
    interior = counts.iloc[1:-1] if len(counts) > 2 else counts
    report.incomplete_sessions = [
        (str(day), int(count))
        for day, count in interior.items()
        if count != RTH_BARS_PER_SESSION
    ]
    return frame, report


__all__ = [
    "BAR_COLUMNS",
    "DEFAULT_CHUNK_DURATION",
    "DEFAULT_REQUEST_SPACING_SECONDS",
    "HistoricalFetchReport",
    "RTH_BARS_PER_SESSION",
    "bars_to_frame",
    "fetch_umc_1m_history",
    "session_days",
]
