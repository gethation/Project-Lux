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
    ccf: QuoteLike
    umc: QuoteLike
    usd_twd: QuoteLike


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
    ccf_book_stale_seconds: float,
    last_ccf_close: float | None,
) -> TradableSpreadSnapshot:
    mid_spread = estimate_mid_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        last_ccf_close=last_ccf_close,
    )
    short_spread, short_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        ccf_book_stale_seconds=ccf_book_stale_seconds,
        umc_side="bid",
        usd_twd_side="bid",
        ccf_side="ask",
    )
    long_spread, long_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        ccf_book_stale_seconds=ccf_book_stale_seconds,
        umc_side="ask",
        usd_twd_side="ask",
        ccf_side="bid",
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
    last_ccf_close: float | None,
) -> float | None:
    observed = ensure_taipei(observed_at)
    if not quote_is_fresh(quote_set.umc, observed, stale_seconds):
        return None
    if not quote_is_fresh(quote_set.usd_twd, observed, stale_seconds):
        return None

    ccf_price = last_ccf_close
    if quote_is_fresh(quote_set.ccf, observed, stale_seconds):
        ccf_price = quote_set.ccf.price
    if ccf_price is None:
        return None

    umc_twd_fair = quote_set.umc.price * quote_set.usd_twd.price / 5.0
    return spread_from_prices(umc_twd_fair, ccf_price)


def estimate_directional_spread(
    quote_set: QuoteSetLike,
    observed_at: Any,
    *,
    stale_seconds: float,
    ccf_book_stale_seconds: float,
    umc_side: str,
    usd_twd_side: str,
    ccf_side: str,
) -> tuple[float | None, str | None]:
    observed = ensure_taipei(observed_at)
    for name, quote in (
        ("umc", quote_set.umc),
        ("usd_twd", quote_set.usd_twd),
    ):
        if not quote_is_fresh(quote, observed, stale_seconds):
            return None, f"stale_{name}"
    if ccf_book_quote_missing(quote_set.ccf):
        return None, "stale_ccf"
    if not quote_is_fresh(quote_set.ccf, observed, ccf_book_stale_seconds):
        return None, "stale_ccf"

    umc_price = book_price(quote_set.umc, umc_side)
    usd_twd_price = book_price(quote_set.usd_twd, usd_twd_side)
    ccf_price = book_price(quote_set.ccf, ccf_side)
    if umc_price is None or usd_twd_price is None or ccf_price is None:
        return None, "missing_book"

    umc_twd_fair = umc_price * usd_twd_price / 5.0
    return spread_from_prices(umc_twd_fair, ccf_price), None


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


def ccf_book_quote_missing(quote: QuoteLike) -> bool:
    return isinstance(quote.raw, dict) and quote.raw.get("book_missing") is True


def book_price(quote: QuoteLike, side: str) -> float | None:
    if side == "bid":
        return quote.bid
    if side == "ask":
        return quote.ask
    raise ValueError(f"Unsupported book side: {side}")


def spread_from_prices(umc_twd_fair: float, ccf_price: float) -> float:
    return (umc_twd_fair - ccf_price) / (umc_twd_fair + ccf_price) * 200.0
