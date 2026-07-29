"""Twelve Data provider: parsing, error surfaces, and the /quote trap."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.integrations.twelvedata import (
    TwelveDataError,
    TwelveDataMarketData,
    TwelveDataRateLimited,
)


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records every call so tests can assert which endpoint was used."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if not self._responses:
            raise AssertionError(f"unexpected extra request to {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def bar(datetime_text: str, close: str) -> dict:
    return {
        "datetime": datetime_text,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
    }


def provider(*responses) -> tuple[TwelveDataMarketData, FakeSession]:
    session = FakeSession(*responses)
    return TwelveDataMarketData("test-key", session=session), session


def test_quote_reads_time_series_not_quote_endpoint() -> None:
    """/quote returns a date-only datetime for forex, so freshness from it is fiction."""
    client, session = provider(FakeResponse({"values": [bar("2026-07-24 06:41:00", "32.32544")]}))

    client.fetch_quote("USD/TWD")

    url, params = session.calls[0]
    assert url.endswith("/time_series")
    assert params["interval"] == "1min"
    assert params["timezone"] == "Asia/Taipei"
    assert params["apikey"] == "test-key"


def test_quote_is_stamped_at_bar_close_not_bar_label() -> None:
    client, _ = provider(FakeResponse({"values": [bar("2026-07-24 06:41:00", "32.32544")]}))

    quote = client.fetch_quote("USD/TWD")

    # The 06:41 label covers 06:41:00-06:42:00, so the value is known as of 06:42.
    assert quote.timestamp == datetime(2026, 7, 24, 6, 42, tzinfo=TAIPEI_TZ)
    assert quote.price == pytest.approx(32.32544)
    assert quote.source == "twelvedata"
    assert quote.symbol == "USD/TWD"


def test_quote_records_its_own_lag() -> None:
    client, _ = provider(FakeResponse({"values": [bar("2026-07-24 06:41:00", "32.3")]}))

    quote = client.fetch_quote("USD/TWD")

    assert quote.raw["bar_open"].startswith("2026-07-24T06:41")
    assert quote.raw["bar_close"].startswith("2026-07-24T06:42")
    assert "source_lag_seconds" in quote.raw


def test_rate_limit_is_a_distinct_exception() -> None:
    client, _ = provider(
        FakeResponse({"status": "error", "code": 429, "message": "run out of credits"})
    )

    with pytest.raises(TwelveDataRateLimited, match="credit limit"):
        client.fetch_quote("USD/TWD")


def test_other_api_errors_raise_the_base_error() -> None:
    client, _ = provider(
        FakeResponse({"status": "error", "code": 404, "message": "symbol not found"})
    )

    with pytest.raises(TwelveDataError, match="404"):
        client.fetch_quote("NOPE/XXX")


def test_error_payloads_arrive_with_http_200() -> None:
    """Status code alone cannot distinguish success from failure here."""
    client, _ = provider(
        FakeResponse(
            {"status": "error", "code": 400, "message": "bad"},
            status_code=200,
        )
    )

    with pytest.raises(TwelveDataError):
        client.fetch_quote("USD/TWD")


def test_non_json_response_is_reported_with_the_body() -> None:
    client, _ = provider(FakeResponse(None, status_code=502, text="<html>gateway</html>"))

    with pytest.raises(TwelveDataError, match="non-JSON"):
        client.fetch_quote("USD/TWD")


def test_empty_values_is_an_error_not_an_empty_quote() -> None:
    client, _ = provider(FakeResponse({"values": []}))

    with pytest.raises(TwelveDataError, match="no bars"):
        client.fetch_quote("USD/TWD")


def test_missing_api_key_is_reported_before_any_request(monkeypatch) -> None:
    monkeypatch.delenv("TWELVEDATA_API_KEY", raising=False)

    with pytest.raises(TwelveDataError, match="TWELVEDATA_API_KEY"):
        TwelveDataMarketData()


def test_ohlcv_returns_ascending_closes_within_the_window() -> None:
    client, _ = provider(
        FakeResponse(
            {
                "values": [
                    bar("2026-07-24 06:03:00", "32.33"),
                    bar("2026-07-24 06:02:00", "32.32"),
                    bar("2026-07-24 06:01:00", "32.31"),
                ]
            }
        )
    )

    frame = client.fetch_ohlcv_1m(
        "USD/TWD",
        datetime(2026, 7, 24, 6, 1, tzinfo=TAIPEI_TZ),
        datetime(2026, 7, 24, 6, 3, tzinfo=TAIPEI_TZ),
    )

    assert list(frame.columns) == ["timestamp", "close"]
    assert len(frame) == 3
    assert frame["close"].tolist() == [32.31, 32.32, 32.33]
    assert frame["timestamp"].is_monotonic_increasing


def test_ohlcv_clips_bars_outside_the_requested_window() -> None:
    client, _ = provider(
        FakeResponse(
            {
                "values": [
                    bar("2026-07-24 06:05:00", "32.35"),
                    bar("2026-07-24 06:02:00", "32.32"),
                    bar("2026-07-24 05:00:00", "32.20"),
                ]
            }
        )
    )

    frame = client.fetch_ohlcv_1m(
        "USD/TWD",
        datetime(2026, 7, 24, 6, 1, tzinfo=TAIPEI_TZ),
        datetime(2026, 7, 24, 6, 3, tzinfo=TAIPEI_TZ),
    )

    assert frame["close"].tolist() == [32.32]


def test_ohlcv_returns_empty_frame_when_the_window_has_no_bars() -> None:
    client, _ = provider(FakeResponse({"values": []}))

    frame = client.fetch_ohlcv_1m(
        "USD/TWD",
        datetime(2026, 7, 25, 6, 0, tzinfo=TAIPEI_TZ),  # a Saturday
        datetime(2026, 7, 25, 7, 0, tzinfo=TAIPEI_TZ),
    )

    assert frame.empty
    assert list(frame.columns) == ["timestamp", "close"]


def test_ohlcv_rejects_a_reversed_window() -> None:
    client, _ = provider()

    with pytest.raises(ValueError, match="start must not be after end"):
        client.fetch_ohlcv_1m(
            "USD/TWD",
            datetime(2026, 7, 24, 7, 0, tzinfo=TAIPEI_TZ),
            datetime(2026, 7, 24, 6, 0, tzinfo=TAIPEI_TZ),
        )


def test_ohlcv_stops_when_a_chunk_repeats_itself() -> None:
    """A source that ignores end_date must not spin forever."""
    repeated = {"values": [bar("2026-07-24 06:02:00", "32.32")]}
    client, session = provider(
        FakeResponse(dict(repeated)),
        FakeResponse(dict(repeated)),
    )

    frame = client.fetch_ohlcv_1m(
        "USD/TWD",
        datetime(2026, 7, 20, 0, 0, tzinfo=TAIPEI_TZ),
        datetime(2026, 7, 24, 6, 3, tzinfo=TAIPEI_TZ),
    )

    assert len(frame) == 1
    assert len(session.calls) <= 2
