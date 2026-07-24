"""IBKR warmup-history window semantics, pinned after a live failure.

IBKR's endDateTime bounds bars by their CLOSE time: a 1m bar labelled T spans
T..T+1min, so requesting end=T silently excludes the bar labelled T. The first
real multi-pair dry-run died on exactly this -- the warmup index ends at 03:59
(the session's last bar label) and the fetched frame ended at 03:58.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.integrations.ibkr.market_data import IbkrUmcQuoteProvider


class FakeClient:
    """Serves 1m bars like IBKR does: only bars whose CLOSE is <= end."""

    def __init__(self, session_start: datetime, session_end: datetime) -> None:
        self.session_start = session_start
        self.session_end = session_end
        self.requests: list[dict] = []

    def fetch_umc_historical_1m(self, *, end_date_time, duration, use_rth=True):
        self.requests.append(
            {"end_date_time": end_date_time, "duration": duration}
        )
        end_utc = datetime.strptime(end_date_time, "%Y%m%d-%H:%M:%S")
        rows = []
        minute = self.session_start
        while minute < self.session_end:
            closes_at = minute + timedelta(minutes=1)
            if closes_at.astimezone(TAIPEI_TZ).replace(tzinfo=None) <= (
                end_utc + timedelta(hours=8)
            ):
                rows.append(
                    {
                        "date": minute.isoformat(),
                        "open": 20.0,
                        "high": 20.0,
                        "low": 20.0,
                        "close": 20.0,
                        "volume": 1.0,
                    }
                )
            minute += timedelta(minutes=1)
        return rows


def provider_with_last_night_session() -> tuple[IbkrUmcQuoteProvider, FakeClient]:
    client = FakeClient(
        session_start=datetime(2026, 7, 23, 21, 30, tzinfo=TAIPEI_TZ),
        session_end=datetime(2026, 7, 24, 4, 0, tzinfo=TAIPEI_TZ),
    )
    return IbkrUmcQuoteProvider(client=client), client


def test_the_last_bar_label_of_the_window_is_included() -> None:
    """end=03:59 must return the bar LABELLED 03:59 (which closes at 04:00)."""
    provider, _ = provider_with_last_night_session()

    frame = provider.fetch_ohlcv_1m(
        "UMC",
        datetime(2026, 7, 23, 21, 30, tzinfo=TAIPEI_TZ),
        datetime(2026, 7, 24, 3, 59, tzinfo=TAIPEI_TZ),
    )

    assert len(frame) == 390
    assert frame["timestamp"].iloc[-1].to_pydatetime() == datetime(
        2026, 7, 24, 3, 59, tzinfo=TAIPEI_TZ
    )


def test_no_bars_past_the_window_leak_through() -> None:
    provider, _ = provider_with_last_night_session()

    frame = provider.fetch_ohlcv_1m(
        "UMC",
        datetime(2026, 7, 23, 21, 30, tzinfo=TAIPEI_TZ),
        datetime(2026, 7, 24, 3, 30, tzinfo=TAIPEI_TZ),
    )

    assert frame["timestamp"].iloc[-1].to_pydatetime() == datetime(
        2026, 7, 24, 3, 30, tzinfo=TAIPEI_TZ
    )


def test_venue_dispatch_constructs_the_real_class(tmp_path) -> None:
    """Regression: the first dry-run died on an ImportError because the
    dispatch named a class that does not exist and no test exercised the
    lazy-import branch."""
    from dataclasses import replace

    from conftest import make_app_config
    from lux_trader.runtime.live.providers import build_us_leg_provider

    config = make_app_config(tmp_path, validate_expected_zscore=False)
    pair = replace(
        config.pairs[0],
        us_leg=replace(config.pairs[0].us_leg, venue="ibkr", symbol="UMC"),
    )
    config = replace(config, pairs=(pair,), active_pair_id=pair.id)

    provider = build_us_leg_provider(config)

    assert type(provider).__name__ == "IbkrUmcQuoteProvider"
    provider.client.close()
