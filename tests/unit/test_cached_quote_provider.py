"""Cache behaviour for quote sources the live loop would otherwise overrun."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from lux_trader.core.time import TAIPEI_TZ
from lux_trader.market_data.cached_quote import CachedQuoteProvider
from lux_trader.market_data.types import LiveQuote


class StubClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class CountingProvider:
    def __init__(self, *, fail_after: int | None = None) -> None:
        self.calls = 0
        self.fail_after = fail_after
        self.price = 32.30

    def fetch_quote(self, symbol: str) -> LiveQuote:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("upstream down")
        self.price += 0.01
        return LiveQuote(
            source="stub",
            symbol=symbol,
            timestamp=datetime(2026, 7, 24, 6, 42, tzinfo=TAIPEI_TZ),
            price=self.price,
        )


def build(fail_after: int | None = None, **kwargs):
    clock = StubClock(datetime(2026, 7, 24, 6, 42, tzinfo=TAIPEI_TZ))
    inner = CountingProvider(fail_after=fail_after)
    cache = CachedQuoteProvider(inner, ttl_seconds=300.0, clock=clock, **kwargs)
    return cache, inner, clock


def test_first_call_reaches_upstream() -> None:
    cache, inner, _ = build()

    cache.fetch_quote("USD/TWD")

    assert inner.calls == 1


def test_repeat_calls_inside_the_ttl_do_not() -> None:
    """The live loop polls every second; 300 polls must cost one credit."""
    cache, inner, clock = build()

    for _ in range(300):
        cache.fetch_quote("USD/TWD")
        clock.advance(1)

    assert inner.calls == 1
    assert cache.hits == 299


def test_upstream_is_called_again_once_the_ttl_expires() -> None:
    cache, inner, clock = build()

    cache.fetch_quote("USD/TWD")
    clock.advance(300)
    cache.fetch_quote("USD/TWD")

    assert inner.calls == 2


def test_cached_quote_keeps_its_original_timestamp() -> None:
    """Restamping would make every downstream staleness check vacuous."""
    cache, _, clock = build()

    first = cache.fetch_quote("USD/TWD")
    clock.advance(299)
    second = cache.fetch_quote("USD/TWD")

    assert second.timestamp == first.timestamp
    assert second is first


def test_switching_symbol_invalidates_rather_than_returning_the_wrong_rate() -> None:
    cache, inner, _ = build()

    cache.fetch_quote("USD/TWD")
    other = cache.fetch_quote("EUR/TWD")

    assert inner.calls == 2
    assert other.symbol == "EUR/TWD"


def test_upstream_failure_serves_the_last_good_quote() -> None:
    cache, _, clock = build(fail_after=1)

    first = cache.fetch_quote("USD/TWD")
    clock.advance(301)
    served = cache.fetch_quote("USD/TWD")

    assert served is first
    assert cache.stale_serves == 1


def test_upstream_failure_with_no_cache_propagates() -> None:
    cache, _, _ = build(fail_after=0)

    with pytest.raises(RuntimeError, match="upstream down"):
        cache.fetch_quote("USD/TWD")


def test_max_serve_turns_a_prolonged_outage_into_an_error() -> None:
    cache, _, clock = build(fail_after=1, max_serve_seconds=600.0)

    cache.fetch_quote("USD/TWD")
    clock.advance(301)
    cache.fetch_quote("USD/TWD")  # still inside max_serve

    clock.advance(400)  # now 701s since the last good fetch
    with pytest.raises(RuntimeError, match="upstream down"):
        cache.fetch_quote("USD/TWD")


def test_max_serve_below_ttl_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="max_serve_seconds"):
        CachedQuoteProvider(CountingProvider(), ttl_seconds=300.0, max_serve_seconds=60.0)


def test_non_positive_ttl_is_rejected() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        CachedQuoteProvider(CountingProvider(), ttl_seconds=0.0)


def test_stats_expose_what_the_cache_did() -> None:
    cache, _, clock = build()

    cache.fetch_quote("USD/TWD")
    clock.advance(1)
    cache.fetch_quote("USD/TWD")

    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["ttl_seconds"] == 300.0
    assert stats["cached_symbol"] == "USD/TWD"


def test_a_full_umc_session_costs_the_expected_credits() -> None:
    """390 minutes at a 300s TTL must stay inside the 800/day free tier."""
    cache, inner, clock = build()

    for _ in range(390 * 60):  # one poll per second for a whole session
        cache.fetch_quote("USD/TWD")
        clock.advance(1)

    assert inner.calls == 78
