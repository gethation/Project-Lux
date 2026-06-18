from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any

from .indicator import IndicatorEngine
from .live_market_data import LiveQuote, LiveQuoteSet, ensure_taipei


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
    quote_set: LiveQuoteSet,
    observed_at: Any,
    indicator: IndicatorEngine,
    *,
    stale_seconds: float,
    last_qff_close: float | None,
) -> TradableSpreadSnapshot:
    mid_spread = estimate_mid_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        last_qff_close=last_qff_close,
    )
    short_spread, short_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        tsm_side="bid",
        usdttwd_side="bid",
        qff_side="ask",
    )
    long_spread, long_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        tsm_side="ask",
        usdttwd_side="ask",
        qff_side="bid",
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
    quote_set: LiveQuoteSet,
    observed_at: Any,
    *,
    stale_seconds: float,
    last_qff_close: float | None,
) -> float | None:
    observed = ensure_taipei(observed_at)
    if not quote_is_fresh(quote_set.tsm, observed, stale_seconds):
        return None
    if not quote_is_fresh(quote_set.usdttwd, observed, stale_seconds):
        return None

    qff_price = last_qff_close
    if quote_is_fresh(quote_set.qff, observed, stale_seconds):
        qff_price = quote_set.qff.price
    if qff_price is None:
        return None

    tsm_twd_fair = quote_set.tsm.price * quote_set.usdttwd.price / 5.0
    return spread_from_prices(tsm_twd_fair, qff_price)


def estimate_directional_spread(
    quote_set: LiveQuoteSet,
    observed_at: Any,
    *,
    stale_seconds: float,
    tsm_side: str,
    usdttwd_side: str,
    qff_side: str,
) -> tuple[float | None, str | None]:
    observed = ensure_taipei(observed_at)
    for name, quote in (
        ("tsm", quote_set.tsm),
        ("usdttwd", quote_set.usdttwd),
        ("qff", quote_set.qff),
    ):
        if not quote_is_fresh(quote, observed, stale_seconds):
            return None, f"stale_{name}"

    tsm_price = book_price(quote_set.tsm, tsm_side)
    usdttwd_price = book_price(quote_set.usdttwd, usdttwd_side)
    qff_price = book_price(quote_set.qff, qff_side)
    if tsm_price is None or usdttwd_price is None or qff_price is None:
        return None, "missing_book"

    tsm_twd_fair = tsm_price * usdttwd_price / 5.0
    return spread_from_prices(tsm_twd_fair, qff_price), None


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


def quote_is_fresh(quote: LiveQuote, observed_at: Any, stale_seconds: float) -> bool:
    age = abs((ensure_taipei(observed_at) - ensure_taipei(quote.timestamp)).total_seconds())
    return age <= stale_seconds


def book_price(quote: LiveQuote, side: str) -> float | None:
    if side == "bid":
        return quote.bid
    if side == "ask":
        return quote.ask
    raise ValueError(f"Unsupported book side: {side}")


def spread_from_prices(tsm_twd_fair: float, qff_price: float) -> float:
    return (tsm_twd_fair - qff_price) / (tsm_twd_fair + qff_price) * 200.0
