from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from lux_trader.integrations.ibkr.historical import (
    BAR_COLUMNS,
    RTH_BARS_PER_SESSION,
    bars_to_frame,
    fetch_umc_1m_history,
    session_days,
)


def rth_session_rows(market_date: str, *, count: int = RTH_BARS_PER_SESSION):
    """One RTH session as the worker would return it: UTC-aware minute bars."""
    open_utc = pd.Timestamp(f"{market_date} 13:30:00", tz="UTC")
    return [
        {
            "date": (open_utc + pd.Timedelta(minutes=i)).to_pydatetime(),
            "open": 20.0,
            "high": 20.5,
            "low": 19.5,
            "close": 20.25,
            "volume": 1000.0,
        }
        for i in range(count)
    ]


class FakeClient:
    """Serves pre-canned chunks and records how it was called."""

    def __init__(self, chunks: list[list[dict[str, Any]]]) -> None:
        self.chunks = list(chunks)
        self.calls: list[dict[str, Any]] = []

    def fetch_umc_historical_1m(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return self.chunks.pop(0) if self.chunks else []


def test_bars_to_frame_normalizes_to_taipei_and_poc_schema() -> None:
    frame = bars_to_frame(rth_session_rows("2026-07-22", count=3))

    assert list(frame.columns) == BAR_COLUMNS
    assert str(frame["timestamp"].dt.tz) == "Asia/Taipei"
    # 13:30 UTC is 21:30 Taipei the same day
    assert frame["timestamp"].iloc[0].strftime("%Y-%m-%d %H:%M") == "2026-07-22 21:30"


def test_bars_to_frame_handles_no_rows() -> None:
    frame = bars_to_frame([])
    assert frame.empty
    assert list(frame.columns) == BAR_COLUMNS


def test_session_days_keeps_a_midnight_crossing_session_together() -> None:
    frame = bars_to_frame(rth_session_rows("2026-07-22"))
    # Taipei 21:30 -> 04:00 next day, but it is one US market session
    assert frame["timestamp"].iloc[0].day != frame["timestamp"].iloc[-1].day
    assert session_days(frame).nunique() == 1


def test_fetch_walks_backwards_and_merges_chunks() -> None:
    client = FakeClient(
        [rth_session_rows("2026-07-22"), rth_session_rows("2026-07-21")]
    )
    frame, report = fetch_umc_1m_history(
        client,
        months=1,
        chunk_duration="1 D",
        end=datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=lambda _seconds: None,
        reporter=lambda _line: None,
    )

    assert report.sessions == 2
    assert report.unique_bars == RTH_BARS_PER_SESSION * 2
    assert frame["timestamp"].is_monotonic_increasing
    # the cursor must move backwards, not repeat the same window
    ends = [call["end_date_time"] for call in client.calls]
    assert ends[0] > ends[1]


def test_fetch_requests_trades_rth_only() -> None:
    client = FakeClient([rth_session_rows("2026-07-22")])
    fetch_umc_1m_history(
        client,
        months=1,
        end=datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=lambda _seconds: None,
        reporter=lambda _line: None,
    )
    assert client.calls[0]["use_rth"] is True


def test_fetch_paces_between_chunks_but_not_before_the_first() -> None:
    client = FakeClient(
        [rth_session_rows("2026-07-22"), rth_session_rows("2026-07-21")]
    )
    slept: list[float] = []
    _frame, report = fetch_umc_1m_history(
        client,
        months=1,
        chunk_duration="1 D",
        request_spacing_seconds=11.0,
        end=datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=slept.append,
        reporter=lambda _line: None,
    )
    # every request but the first is preceded by one pause of the configured size
    assert slept == [11.0] * (report.chunks_requested - 1)
    assert report.chunks_requested >= 2


def test_fetch_drops_duplicate_minutes_across_overlapping_chunks() -> None:
    session = rth_session_rows("2026-07-22")
    client = FakeClient([session, session])
    frame, report = fetch_umc_1m_history(
        client,
        months=1,
        chunk_duration="1 D",
        end=datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=lambda _seconds: None,
        reporter=lambda _line: None,
    )
    assert report.unique_bars == RTH_BARS_PER_SESSION
    assert frame["timestamp"].is_unique


def test_fetch_flags_a_short_interior_session() -> None:
    client = FakeClient(
        [
            rth_session_rows("2026-07-22"),
            rth_session_rows("2026-07-21", count=200),
            rth_session_rows("2026-07-20"),
        ]
    )
    _frame, report = fetch_umc_1m_history(
        client,
        months=1,
        chunk_duration="1 D",
        end=datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=lambda _seconds: None,
        reporter=lambda _line: None,
    )
    assert [count for _day, count in report.incomplete_sessions] == [200]


def test_fetch_stops_on_an_empty_chunk() -> None:
    client = FakeClient([rth_session_rows("2026-07-22"), []])
    _frame, report = fetch_umc_1m_history(
        client,
        months=6,
        chunk_duration="1 D",
        end=datetime(2026, 7, 23, tzinfo=UTC),
        sleeper=lambda _seconds: None,
        reporter=lambda _line: None,
    )
    assert report.chunks_requested == 2


def test_fetch_rejects_nonsense_arguments() -> None:
    client = FakeClient([])
    for kwargs in ({"months": 0}, {"request_spacing_seconds": -1.0}):
        with pytest.raises(ValueError):
            fetch_umc_1m_history(client, **kwargs)


def test_end_date_time_is_sent_as_utc() -> None:
    client = FakeClient([rth_session_rows("2026-07-22")])
    fetch_umc_1m_history(
        client,
        months=1,
        end=datetime(2026, 7, 23, 4, 0, tzinfo=UTC) + timedelta(0),
        sleeper=lambda _seconds: None,
        reporter=lambda _line: None,
    )
    # the Gateway's own timezone setting must never be able to reinterpret this
    assert client.calls[0]["end_date_time"] == "20260723-04:00:00"
