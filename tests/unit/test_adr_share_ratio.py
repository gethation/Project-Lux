"""The ADR share ratio must reach the spread, not just position sizing.

It used to appear as a literal 5.0 in six places while sizing read the configured
value. That was correct only for as long as every pair happened to use 5.0, which
both current pairs do -- so nothing was broken, and nothing would have caught it
breaking. These tests use a ratio of 4.0 precisely because no real pair does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.core.tradable_spread import (
    estimate_mid_spread,
    spread_from_prices,
    us_leg_twd_fair_price,
)
from lux_trader.market_data.minute_bar import LiveMinuteBarBuilder
from lux_trader.market_data.types import LiveQuote


MINUTE = datetime(2026, 7, 24, 22, 30, tzinfo=TAIPEI_TZ)
CLOSE = MINUTE + timedelta(minutes=1)

US_PRICE = 20.0
FX = 32.0
TW_PRICE = 150.0


def quote(name: str, price: float, *, bid: float | None = None, ask: float | None = None):
    return LiveQuote(
        source=name,
        symbol=name,
        timestamp=CLOSE,
        price=price,
        bid=bid if bid is not None else price - 0.01,
        ask=ask if ask is not None else price + 0.01,
    )


class Quotes:
    def __init__(self) -> None:
        self.tw_leg = quote("tw_leg", TW_PRICE)
        self.us_leg = quote("us_leg", US_PRICE)
        self.usdttwd = quote("usdttwd", FX)


def test_fair_price_divides_by_the_ratio() -> None:
    assert us_leg_twd_fair_price(20.0, 32.0, 5.0) == pytest.approx(128.0)
    assert us_leg_twd_fair_price(20.0, 32.0, 4.0) == pytest.approx(160.0)


def test_a_non_positive_ratio_is_rejected() -> None:
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError, match="adr_share_ratio"):
            us_leg_twd_fair_price(20.0, 32.0, bad)


def test_minute_bar_spread_follows_the_configured_ratio() -> None:
    spreads = {}
    for ratio in (4.0, 5.0):
        builder = LiveMinuteBarBuilder(
            stale_seconds=10.0,
            max_leg_timestamp_skew_seconds=10.0,
            adr_share_ratio=ratio,
        )
        builder.current_minute = MINUTE
        quotes = Quotes()
        builder.current_quotes = {
            "tw_leg": quotes.tw_leg,
            "us_leg": quotes.us_leg,
            "usdttwd": quotes.usdttwd,
        }
        result = builder._finalize_current_minute()
        assert result.bar is not None, result.skipped_reason
        spreads[ratio] = result.bar.spread
        expected_fair = US_PRICE * FX / ratio
        assert result.bar.us_leg_twd_fair == pytest.approx(expected_fair)
        assert result.bar.spread == pytest.approx(
            spread_from_prices(expected_fair, TW_PRICE)
        )

    # The whole point: a different ratio must produce a different spread.
    assert spreads[4.0] != pytest.approx(spreads[5.0])


def test_mid_spread_follows_the_configured_ratio() -> None:
    quotes = Quotes()
    four = estimate_mid_spread(
        quotes,
        CLOSE,
        stale_seconds=10.0,
        last_tw_leg_close=TW_PRICE,
        adr_share_ratio=4.0,
    )
    five = estimate_mid_spread(
        quotes,
        CLOSE,
        stale_seconds=10.0,
        last_tw_leg_close=TW_PRICE,
        adr_share_ratio=5.0,
    )

    assert four == pytest.approx(spread_from_prices(US_PRICE * FX / 4.0, TW_PRICE))
    assert five == pytest.approx(spread_from_prices(US_PRICE * FX / 5.0, TW_PRICE))
    assert four != pytest.approx(five)


def test_the_default_still_matches_the_frozen_baseline_ratio() -> None:
    """Every committed config uses 5.0; the defaults must not quietly differ."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        max_leg_timestamp_skew_seconds=10.0,
    )

    assert builder.adr_share_ratio == 5.0


def test_config_carries_the_ratio_to_both_consumers() -> None:
    from pathlib import Path

    from lux_trader.config import load_config

    repo_root = Path(__file__).resolve().parents[2]
    config = load_config(repo_root / "configs" / "replay.fixture.toml")

    # Sizing and the spread must read the same field, which was the actual defect.
    assert config.active_pair.us_leg.adr_share_ratio == 5.0
