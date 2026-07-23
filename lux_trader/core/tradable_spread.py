from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Any, Protocol

from .indicator import IndicatorEngine
from .time import ensure_taipei


class QuoteLike(Protocol):
    timestamp: datetime
    price: float
    bid: float | None
    ask: float | None
    raw: Any


class QuoteSetLike(Protocol):
    tw_leg: QuoteLike
    us_leg: QuoteLike
    usdttwd: QuoteLike


@dataclass(frozen=True)
class TradableSpreadSnapshot:
    mid_spread: float | None
    mid_zscore: float | None
    short_spread: float | None
    short_zscore: float | None
    long_spread: float | None
    long_zscore: float | None
    missing_reason: str | None = None


def estimate_tradable_spreads(
    quote_set: QuoteSetLike,
    observed_at: Any,
    indicator: IndicatorEngine,
    *,
    stale_seconds: float,
    tw_leg_book_stale_seconds: float,
    last_tw_leg_close: float | None,
) -> TradableSpreadSnapshot:
    mid_spread = estimate_mid_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        last_tw_leg_close=last_tw_leg_close,
    )
    short_spread, short_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        tw_leg_book_stale_seconds=tw_leg_book_stale_seconds,
        us_leg_side="bid",
        usdttwd_side="bid",
        tw_leg_side="ask",
    )
    long_spread, long_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        tw_leg_book_stale_seconds=tw_leg_book_stale_seconds,
        us_leg_side="ask",
        usdttwd_side="ask",
        tw_leg_side="bid",
    )
    missing_reason = short_missing or long_missing
    return TradableSpreadSnapshot(
        mid_spread=mid_spread,
        mid_zscore=estimate_zscore(indicator, mid_spread),
        short_spread=short_spread,
        short_zscore=estimate_zscore(indicator, short_spread),
        long_spread=long_spread,
        long_zscore=estimate_zscore(indicator, long_spread),
        missing_reason=missing_reason,
    )


def estimate_mid_spread(
    quote_set: QuoteSetLike,
    observed_at: Any,
    *,
    stale_seconds: float,
    last_tw_leg_close: float | None,
) -> float | None:
    observed = ensure_taipei(observed_at)
    if not quote_is_fresh(quote_set.us_leg, observed, stale_seconds):
        return None
    if not quote_is_fresh(quote_set.usdttwd, observed, stale_seconds):
        return None

    tw_leg_price = last_tw_leg_close
    if quote_is_fresh(quote_set.tw_leg, observed, stale_seconds):
        tw_leg_price = quote_set.tw_leg.price
    if tw_leg_price is None:
        return None

    us_leg_twd_fair = quote_set.us_leg.price * quote_set.usdttwd.price / 5.0
    return spread_from_prices(us_leg_twd_fair, tw_leg_price)


def estimate_directional_spread(
    quote_set: QuoteSetLike,
    observed_at: Any,
    *,
    stale_seconds: float,
    tw_leg_book_stale_seconds: float,
    us_leg_side: str,
    usdttwd_side: str,
    tw_leg_side: str,
) -> tuple[float | None, str | None]:
    observed = ensure_taipei(observed_at)
    for name, quote in (
        ("us_leg", quote_set.us_leg),
        ("usdttwd", quote_set.usdttwd),
    ):
        if not quote_is_fresh(quote, observed, stale_seconds):
            return None, f"stale_{name}"
    if tw_leg_book_quote_missing(quote_set.tw_leg):
        return None, "stale_tw_leg"
    if not quote_is_fresh(quote_set.tw_leg, observed, tw_leg_book_stale_seconds):
        return None, "stale_tw_leg"

    us_leg_price = book_price(quote_set.us_leg, us_leg_side)
    usdttwd_price = book_price(quote_set.usdttwd, usdttwd_side)
    tw_leg_price = book_price(quote_set.tw_leg, tw_leg_side)
    if us_leg_price is None or usdttwd_price is None or tw_leg_price is None:
        return None, "missing_book"

    us_leg_twd_fair = us_leg_price * usdttwd_price / 5.0
    return spread_from_prices(us_leg_twd_fair, tw_leg_price), None


def estimate_zscore(indicator: IndicatorEngine, spread: float | None) -> float | None:
    if spread is None:
        return None
    if len(indicator.values) < indicator.window:
        return None
    mean = indicator.total / indicator.window
    variance = max(indicator.total_sq / indicator.window - mean * mean, 0.0)
    std = sqrt(variance)
    if std == 0.0:
        return None
    return (spread - mean) / std


def quote_is_fresh(quote: QuoteLike, observed_at: Any, stale_seconds: float) -> bool:
    age = abs((ensure_taipei(observed_at) - ensure_taipei(quote.timestamp)).total_seconds())
    return age <= stale_seconds


def tw_leg_book_quote_missing(quote: QuoteLike) -> bool:
    return isinstance(quote.raw, dict) and quote.raw.get("book_missing") is True


def book_price(quote: QuoteLike, side: str) -> float | None:
    if side == "bid":
        return quote.bid
    if side == "ask":
        return quote.ask
    raise ValueError(f"Unsupported book side: {side}")


def spread_from_prices(us_leg_twd_fair: float, tw_leg_price: float) -> float:
    return (us_leg_twd_fair - tw_leg_price) / (us_leg_twd_fair + tw_leg_price) * 200.0
