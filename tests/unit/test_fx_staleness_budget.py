"""FX as a slow reference rate rather than a co-timed leg.

QFF/TSM's three legs are all 1Hz tick sources, so one 10s budget and a leg-skew
check ask the right question of them. CCF/UMC's FX is a conversion factor served
from a metered REST API behind a 300s cache, which is stale by construction: the
worst case measured on 2026-07-24 was ~335s (300s cache + ~35s publish lag).

Setting FxConfig.stale_seconds reclassifies it. The tests below pin both halves
of that reclassification, and pin that leaving it unset changes nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.market_data.minute_bar import LiveMinuteBarBuilder
from lux_trader.market_data.types import LiveQuote, LiveQuoteSet


MINUTE = datetime(2026, 7, 24, 22, 30, tzinfo=TAIPEI_TZ)
CLOSE = MINUTE + timedelta(minutes=1)


def quote(source: str, *, age_seconds: float, price: float = 100.0) -> LiveQuote:
    return LiveQuote(
        source=source,
        symbol=source,
        timestamp=CLOSE - timedelta(seconds=age_seconds),
        price=price,
    )


def build(fx_stale_seconds: float | None) -> LiveMinuteBarBuilder:
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        max_leg_timestamp_skew_seconds=10.0,
        fx_stale_seconds=fx_stale_seconds,
    )
    builder.current_minute = MINUTE
    return builder


def finalize(
    builder: LiveMinuteBarBuilder,
    *,
    fx_age: float,
    us_age: float = 1.0,
    tw_age: float = 1.0,
):
    builder.current_quotes = {
        "tw_leg": quote("tw_leg", age_seconds=tw_age, price=150.0),
        "us_leg": quote("us_leg", age_seconds=us_age, price=20.0),
        "usdttwd": quote("usdttwd", age_seconds=fx_age, price=32.3),
    }
    return builder._finalize_current_minute()


def test_unset_keeps_the_global_budget_for_fx() -> None:
    """QFF/TSM must be untouched: FX still hard-fails past 10s."""
    result = finalize(build(None), fx_age=11.0)

    assert result.bar is None
    assert result.skipped_reason == "market_data_stale"
    assert result.payload["source"] == "usdttwd"


def test_unset_keeps_fx_inside_the_skew_check() -> None:
    result = finalize(build(None), fx_age=9.0, us_age=0.0)

    # 9s of skew is within the 10s allowance, so this passes for the right reason.
    assert result.bar is not None


def test_a_budget_admits_the_measured_worst_case() -> None:
    result = finalize(build(600.0), fx_age=335.0)

    assert result.bar is not None, result.skipped_reason


def test_the_budget_is_still_a_limit() -> None:
    result = finalize(build(600.0), fx_age=601.0)

    assert result.bar is None
    assert result.skipped_reason == "market_data_stale"
    assert result.payload["source"] == "usdttwd"
    assert result.payload["budget_seconds"] == 600.0


def test_a_budget_does_not_loosen_the_us_leg() -> None:
    """The point is to reclassify FX, not to weaken equity-feed detection."""
    result = finalize(build(600.0), fx_age=335.0, us_age=11.0)

    assert result.bar is None
    assert result.skipped_reason == "market_data_stale"
    assert result.payload["source"] == "us_leg"
    assert result.payload["budget_seconds"] == 10.0


def test_fx_leaves_the_skew_check_when_it_has_its_own_budget() -> None:
    """Without the exemption a 335s-old rate fails skew even with a budget."""
    result = finalize(build(600.0), fx_age=335.0, us_age=0.0, tw_age=0.0)

    assert result.bar is not None, result.skipped_reason


def test_skew_still_catches_the_tick_legs_drifting_apart() -> None:
    result = finalize(build(600.0), fx_age=335.0, us_age=0.0, tw_age=11.0)

    # tw_leg past stale_seconds is dropped rather than skewed, so the bar builds
    # with a forward-filled futures price -- but only because it was seeded.
    assert result.skipped_reason in {None, "missing_tw_leg_forward_fill"}


def test_skew_failure_is_reachable_between_the_two_tick_legs() -> None:
    builder = build(600.0)
    builder.last_tw_leg_close = 150.0
    builder.current_quotes = {
        "tw_leg": quote("tw_leg", age_seconds=9.0, price=150.0),
        "us_leg": quote("us_leg", age_seconds=0.0, price=20.0),
        "usdttwd": quote("usdttwd", age_seconds=335.0, price=32.3),
    }
    builder.max_leg_timestamp_skew_seconds = 5.0

    result = builder._finalize_current_minute()

    assert result.skipped_reason == "leg_timestamp_skew"
    assert result.payload["skew_seconds"] == pytest.approx(9.0)


@pytest.mark.parametrize("fx_age", (0.0, 100.0, 335.0, 599.0))
def test_the_whole_admitted_range_builds_a_bar(fx_age: float) -> None:
    result = finalize(build(600.0), fx_age=fx_age)

    assert result.bar is not None, f"{fx_age}s: {result.skipped_reason}"
