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


# ---------------------------------------------------------------------------
# Warmup window end. Measured 2026-07-25 mid-session: IBKR's delayed history
# reached 00:36 while the wall clock said 00:52, so a window ending one minute
# ago asked for twelve minutes of bars that did not exist yet and the merge
# failed. A pair that declares us_leg.stale_seconds is stating how far behind
# its feed can be; the window respects that same declaration.
# ---------------------------------------------------------------------------

from lux_trader.config import LiveMarketDataConfig
from lux_trader.market_data.warmup import WarmupBuilder


def live_config(tmp_path) -> LiveMarketDataConfig:
    return LiveMarketDataConfig(
        polling_seconds=1.0,
        minute_finalize_delay_seconds=1.0,
        stale_seconds=10.0,
        tw_leg_book_stale_seconds=55.0,
        sync_windows_time_on_startup=False,
        clock_skew_fail_seconds=60.0,
        windows_time_sync_timeout_seconds=15.0,
        max_leg_timestamp_skew_seconds=10.0,
        warmup_minutes=10,
        tw_leg_product="CCF",
        tw_leg_symbol="auto",
        binance_symbol="TSM/USDT:USDT",
        bitopro_symbol="USDT/TWD",
        fubon_env_path=None,
        taifex_tw_leg_1m_csv=None,
        taifex_use_network=False,
        taifex_cache_dir=tmp_path / "cache",
    )


class NullProvider:
    def fetch_1m(self, symbol, start, end):
        raise AssertionError("not reached in these tests")

    def fetch_ohlcv_1m(self, symbol, start, end):
        raise AssertionError("not reached in these tests")


def builder_with_lag(tmp_path, lag_seconds):
    return WarmupBuilder(
        live_config=live_config(tmp_path),
        tw_leg_intraday_provider=NullProvider(),
        tw_leg_fallback_provider=None,
        us_leg_provider=NullProvider(),
        usdttwd_provider=NullProvider(),
        us_leg_lag_seconds=lag_seconds,
    )


def captured_end_minute(builder, requested_end):
    """Run build() far enough to see the window end it computed."""
    seen = {}

    def spy(symbol, start, end):
        seen["end"] = end
        raise RuntimeError("stop here")

    builder.tw_leg_intraday_provider.fetch_1m = spy
    try:
        builder.build(tw_leg_symbol="CCFH6", end=requested_end)
    except RuntimeError:
        pass
    return seen["end"]


def test_declared_lag_moves_the_window_end_back(tmp_path) -> None:
    requested = datetime(2026, 7, 25, 0, 52, tzinfo=TAIPEI_TZ)

    end_minute = captured_end_minute(builder_with_lag(tmp_path, 1200.0), requested)

    # 00:52 -> 00:51 (last closed minute) -> minus 20 minutes of declared lag.
    assert end_minute == datetime(2026, 7, 25, 0, 31, tzinfo=TAIPEI_TZ)


def test_no_declared_lag_keeps_the_previous_minute(tmp_path) -> None:
    requested = datetime(2026, 7, 25, 0, 52, tzinfo=TAIPEI_TZ)

    end_minute = captured_end_minute(builder_with_lag(tmp_path, None), requested)

    assert end_minute == datetime(2026, 7, 25, 0, 51, tzinfo=TAIPEI_TZ)


def test_the_measured_gap_is_covered(tmp_path) -> None:
    """The live failure: history reached 00:36, window wanted 00:48."""
    requested = datetime(2026, 7, 25, 0, 49, tzinfo=TAIPEI_TZ)

    end_minute = captured_end_minute(builder_with_lag(tmp_path, 1200.0), requested)

    assert end_minute <= datetime(2026, 7, 25, 0, 36, tzinfo=TAIPEI_TZ)
