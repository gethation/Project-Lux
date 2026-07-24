"""US-leg staleness budget: delayed IBKR data before the NYSE subscription.

Without it the ~15-minute-delayed timestamps fail both the freshness gate and
the leg-skew check, so no CCF/UMC bar or signal can exist at all. Unset keeps
today's strict behaviour -- which is also the correct end state once the
real-time subscription is live.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.time import TAIPEI_TZ
from lux_trader.core.tradable_spread import estimate_tradable_spreads
from lux_trader.market_data.minute_bar import LiveMinuteBarBuilder
from lux_trader.market_data.types import LiveQuote


MINUTE = datetime(2026, 7, 22, 22, 30, tzinfo=TAIPEI_TZ)
CLOSE = MINUTE + timedelta(minutes=1)
DELAY = 900.0  # IBKR delayed feed: ~15 minutes


def quote(name: str, *, age: float, price: float, book: bool = True) -> LiveQuote:
    return LiveQuote(
        source=name,
        symbol=name,
        timestamp=CLOSE - timedelta(seconds=age),
        price=price,
        bid=price - 0.01 if book else None,
        ask=price + 0.01 if book else None,
    )


def ccf_umc_builder() -> LiveMinuteBarBuilder:
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        max_leg_timestamp_skew_seconds=10.0,
        weekend_policy="none",
        fx_stale_seconds=600.0,
        us_stale_seconds=1200.0,
        adr_share_ratio=5.0,
    )
    builder.current_minute = MINUTE
    return builder


def finalize(builder: LiveMinuteBarBuilder, *, us_age: float, tw_age: float = 1.0):
    builder.current_quotes = {
        "tw_leg": quote("tw_leg", age=tw_age, price=150.0),
        "us_leg": quote("us_leg", age=us_age, price=20.0),
        "usdttwd": quote("usdttwd", age=335.0, price=32.3, book=False),
    }
    return builder._finalize_current_minute()


def test_delayed_us_quote_builds_a_bar_with_the_budget() -> None:
    result = finalize(ccf_umc_builder(), us_age=DELAY)

    assert result.bar is not None, result.skipped_reason


def test_without_the_budget_delayed_us_data_is_stale() -> None:
    builder = ccf_umc_builder()
    builder.us_stale_seconds = None

    result = finalize(builder, us_age=DELAY)

    assert result.bar is None
    assert result.skipped_reason == "market_data_stale"
    assert result.payload["source"] == "us_leg"


def test_the_us_budget_is_still_a_limit() -> None:
    result = finalize(ccf_umc_builder(), us_age=1201.0)

    assert result.bar is None
    assert result.payload["source"] == "us_leg"
    assert result.payload["budget_seconds"] == 1200.0


def test_skew_survives_both_legs_reclassified() -> None:
    """With FX and US both budgeted, only CCF remains in the skew set -- one
    (or zero) timestamps must not crash the check, they make it vacuous."""
    result = finalize(ccf_umc_builder(), us_age=DELAY, tw_age=1.0)
    assert result.bar is not None

    # tw_leg stale too: skew set is empty; the bar still builds off the
    # forward-filled futures close.
    builder = ccf_umc_builder()
    builder.last_tw_leg_close = 150.0
    result = finalize(builder, us_age=DELAY, tw_age=60.0)
    assert result.skipped_reason is None
    assert result.bar is not None
    assert result.bar.tw_leg_close is None  # forward-filled, not fresh


def test_qff_tsm_us_skew_behaviour_is_untouched() -> None:
    """Unset us budget: a us/tw timestamp drift past the allowance still fails."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        max_leg_timestamp_skew_seconds=5.0,
    )
    builder.current_minute = MINUTE
    builder.current_quotes = {
        "tw_leg": quote("tw_leg", age=9.0, price=150.0),
        "us_leg": quote("us_leg", age=0.0, price=20.0),
        "usdttwd": quote("usdttwd", age=0.0, price=32.3),
    }

    result = builder._finalize_current_minute()

    assert result.skipped_reason == "leg_timestamp_skew"


def test_decision_path_honours_the_us_budget() -> None:
    class Quotes:
        tw_leg = quote("tw_leg", age=1.0, price=150.0)
        us_leg = quote("us_leg", age=DELAY, price=20.0)
        usdttwd = quote("usdttwd", age=335.0, price=32.3, book=False)

    with_budget = estimate_tradable_spreads(
        Quotes(), CLOSE, IndicatorEngine(window=5),
        stale_seconds=10.0, tw_leg_book_stale_seconds=55.0,
        last_tw_leg_close=150.0, adr_share_ratio=5.0,
        fx_stale_seconds=600.0, us_stale_seconds=1200.0,
    )
    without = estimate_tradable_spreads(
        Quotes(), CLOSE, IndicatorEngine(window=5),
        stale_seconds=10.0, tw_leg_book_stale_seconds=55.0,
        last_tw_leg_close=150.0, adr_share_ratio=5.0,
        fx_stale_seconds=600.0,
    )

    assert with_budget.short_spread is not None
    assert with_budget.long_spread is not None
    # Delayed IBKR quotes still carry a book, so directions differ.
    assert with_budget.short_spread != pytest.approx(with_budget.long_spread)
    assert without.short_spread is None
    assert without.missing_reason == "stale_us_leg"
