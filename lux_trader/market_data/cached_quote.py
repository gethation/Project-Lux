"""Throttle a QuoteProvider whose upstream cannot take the live loop's cadence.

The live loop polls every second. Twelve Data's free tier allows 8 credits per
minute, so an unthrottled provider exhausts the per-minute limit in 8 seconds and
the 800/day limit in about 13 minutes. Sources like this are not usable directly.

The cache is deliberately a separate object rather than a detail inside the
provider, because the policy question -- how stale a rate may be before the pair
should stop trading on it -- is a strategy decision, not a vendor one. Keeping it
here means swapping the upstream source does not relocate that decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..core.time import TAIPEI_TZ
from .types import LiveQuote, QuoteProvider


class CachedQuoteProvider:
    """Serve a cached quote until ``ttl_seconds`` elapse, then refetch.

    The cached LiveQuote keeps its original timestamp. It is not restamped on
    each hit, because a quote's timestamp is a claim about when the underlying
    data was valid, and reissuing it as fresh would make every downstream
    staleness check vacuous -- an upstream stuck serving one value forever would
    look perfectly healthy.
    """

    def __init__(
        self,
        inner: QuoteProvider,
        *,
        ttl_seconds: float,
        max_serve_seconds: float | None = None,
        clock=None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_serve_seconds is not None and max_serve_seconds < ttl_seconds:
            raise ValueError("max_serve_seconds must be at least ttl_seconds")
        self.inner = inner
        self.ttl = timedelta(seconds=float(ttl_seconds))
        # How long a cached quote may still be served after a refetch failure.
        # None means "serve indefinitely"; a value turns a prolonged upstream
        # outage into a hard error instead of a silently frozen rate.
        self.max_serve = (
            None if max_serve_seconds is None else timedelta(seconds=float(max_serve_seconds))
        )
        self.clock = clock or (lambda: datetime.now(TAIPEI_TZ))
        self._cached: LiveQuote | None = None
        self._cached_symbol: str | None = None
        self._fetched_at: datetime | None = None
        self.hits = 0
        self.misses = 0
        self.stale_serves = 0

    def _is_fresh(self, now: datetime) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < self.ttl

    def fetch_quote(self, symbol: str) -> LiveQuote:
        now = self.clock()
        if symbol != self._cached_symbol:
            # A different symbol invalidates the cache outright rather than
            # silently returning the previous instrument's rate.
            self._cached = None
            self._fetched_at = None
        if self._cached is not None and self._is_fresh(now):
            self.hits += 1
            return self._cached

        try:
            quote = self.inner.fetch_quote(symbol)
        except Exception:
            if self._cached is None or self._fetched_at is None:
                raise
            age = now - self._fetched_at
            if self.max_serve is not None and age > self.max_serve:
                raise
            self.stale_serves += 1
            return self._cached

        self.misses += 1
        self._cached = quote
        self._cached_symbol = symbol
        self._fetched_at = now
        return quote

    def stats(self) -> dict[str, float | int | None]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stale_serves": self.stale_serves,
            "ttl_seconds": self.ttl.total_seconds(),
            "cached_symbol": self._cached_symbol,
            "fetched_at": self._fetched_at.isoformat() if self._fetched_at else None,
        }
