from __future__ import annotations

import json
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from lux_trader.integrations.ibkr.calendar import umc_rth_session
from lux_trader.integrations.ibkr.market_data import IbkrUmcQuoteProvider
from lux_trader.store import SQLiteStore


TAIPEI = ZoneInfo("Asia/Taipei")


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.quote_timeout: float | None = None
        self.closed = False

    def fetch_umc_quote(self, *, quote_wait_timeout_seconds: float) -> dict[str, Any]:
        self.quote_timeout = quote_wait_timeout_seconds
        return self.payload

    def session_health(self) -> dict[str, Any]:
        return {"connected": True, "status": "connected"}

    def close(self) -> None:
        self.closed = True


def delayed_payload() -> dict[str, Any]:
    return {
        "con_id": 46_613_372,
        "market_data_tier": 3,
        "last": 21.39,
        "close": 21.29,
        "bid": 21.38,
        "ask": 21.40,
        "bid_size": 10,
        "ask_size": 12,
        "delayed_last_timestamp": 1_753_292_700,
        "last_timestamp": None,
        "ticker_time": datetime(2026, 7, 23, 12, 0, tzinfo=ZoneInfo("UTC")),
        "observed_at": datetime(2026, 7, 23, 20, 0, tzinfo=TAIPEI),
    }


def test_umc_quote_matches_protocol_marks_delayed_and_warns_once() -> None:
    stream = StringIO()
    client = FakeClient(delayed_payload())
    provider = IbkrUmcQuoteProvider(
        client,
        quote_wait_timeout_seconds=7.5,
        warning_stream=stream,
    )

    quote = provider.fetch_quote("UMC")
    provider.fetch_quote("umc")

    assert callable(provider.fetch_quote)
    assert quote.symbol == "UMC"
    assert quote.price == 21.39
    assert quote.bid == 21.38
    assert quote.ask == 21.40
    assert quote.bid_size == 10
    assert quote.ask_size == 12
    assert quote.timestamp.tzinfo == TAIPEI
    assert quote.market_data_tier == 3
    assert quote.is_delayed is True
    assert quote.raw["market_data_tier_label"] == "delayed"
    assert quote.raw["is_delayed"] is True
    assert client.quote_timeout == 7.5
    assert stream.getvalue().count("DELAYED MARKET DATA") == 1
    assert provider.market_data_status() == {
        "market_data_tier": 3,
        "market_data_tier_label": "delayed",
        "is_delayed": True,
    }


def test_umc_quote_rejects_other_symbols() -> None:
    provider = IbkrUmcQuoteProvider(
        FakeClient(delayed_payload()),
        warning_stream=StringIO(),
    )

    with pytest.raises(ValueError, match="does not serve"):
        provider.fetch_quote("TSM")


def test_delayed_tier_is_recorded_by_existing_market_tick_store(
    tmp_path: Path,
) -> None:
    provider = IbkrUmcQuoteProvider(
        FakeClient(delayed_payload()),
        warning_stream=StringIO(),
    )
    quote = provider.fetch_quote("UMC")
    store = SQLiteStore(tmp_path / "ibkr-tier.sqlite3")
    try:
        store.initialize()
        store.record_market_tick(
            quote,
            datetime(2026, 7, 23, 20, 0, tzinfo=TAIPEI),
        )
        store.commit()
        row = store.connection.execute(
            "SELECT raw_json FROM market_ticks WHERE source = 'ibkr_umc'"
        ).fetchone()
        assert row is not None
        raw = json.loads(row["raw_json"])
        assert raw["market_data_tier"] == 3
        assert raw["market_data_tier_label"] == "delayed"
        assert raw["is_delayed"] is True
    finally:
        store.close()


def test_umc_rth_clock_tracks_us_dst_without_taipei_offset() -> None:
    summer = umc_rth_session(date(2026, 7, 23))
    winter = umc_rth_session(date(2026, 1, 23))

    assert summer.opens_at.isoformat() == "2026-07-23T21:30:00+08:00"
    assert summer.closes_at.isoformat() == "2026-07-24T04:00:00+08:00"
    assert winter.opens_at.isoformat() == "2026-01-23T22:30:00+08:00"
    assert winter.closes_at.isoformat() == "2026-01-24T05:00:00+08:00"
