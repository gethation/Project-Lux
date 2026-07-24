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
    adr_share_ratio: float,
    fx_stale_seconds: float | None = None,
    us_stale_seconds: float | None = None,
) -> TradableSpreadSnapshot:
    """Estimate mid and directional spreads from live quotes.

    ``fx_stale_seconds`` is the same reclassification knob as on the minute-bar
    builder (see FxConfig.stale_seconds): None keeps the FX quote a co-timed leg
    with the global budget and book-side pricing; a value gives it its own budget
    and prices it at its single reference rate for both directions -- a metered
    REST source has no book, and the executable-cost information the directional
    estimate exists for lives in the two equity/futures books, not in a 1-2bp FX
    spread.
    """
    mid_spread = estimate_mid_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        last_tw_leg_close=last_tw_leg_close,
        adr_share_ratio=adr_share_ratio,
        fx_stale_seconds=fx_stale_seconds,
        us_stale_seconds=us_stale_seconds,
    )
    short_spread, short_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        tw_leg_book_stale_seconds=tw_leg_book_stale_seconds,
        us_leg_side="bid",
        usdttwd_side="bid",
        tw_leg_side="ask",
        adr_share_ratio=adr_share_ratio,
        fx_stale_seconds=fx_stale_seconds,
        us_stale_seconds=us_stale_seconds,
    )
    long_spread, long_missing = estimate_directional_spread(
        quote_set,
        observed_at,
        stale_seconds=stale_seconds,
        tw_leg_book_stale_seconds=tw_leg_book_stale_seconds,
        us_leg_side="ask",
        usdttwd_side="ask",
        tw_leg_side="bid",
        adr_share_ratio=adr_share_ratio,
        fx_stale_seconds=fx_stale_seconds,
        us_stale_seconds=us_stale_seconds,
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
    adr_share_ratio: float,
    fx_stale_seconds: float | None = None,
    us_stale_seconds: float | None = None,
) -> float | None:
    observed = ensure_taipei(observed_at)
    fx_budget = stale_seconds if fx_stale_seconds is None else fx_stale_seconds
    us_budget = stale_seconds if us_stale_seconds is None else us_stale_seconds
    if not quote_is_fresh(quote_set.us_leg, observed, us_budget):
        return None
    if not quote_is_fresh(quote_set.usdttwd, observed, fx_budget):
        return None

    tw_leg_price = last_tw_leg_close
    if quote_is_fresh(quote_set.tw_leg, observed, stale_seconds):
        tw_leg_price = quote_set.tw_leg.price
    if tw_leg_price is None:
        return None

    us_leg_twd_fair = us_leg_twd_fair_price(
        quote_set.us_leg.price,
        quote_set.usdttwd.price,
        adr_share_ratio,
    )
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
    adr_share_ratio: float,
    fx_stale_seconds: float | None = None,
    us_stale_seconds: float | None = None,
) -> tuple[float | None, str | None]:
    observed = ensure_taipei(observed_at)
    fx_budget = stale_seconds if fx_stale_seconds is None else fx_stale_seconds
    us_budget = stale_seconds if us_stale_seconds is None else us_stale_seconds
    for name, quote, budget in (
        ("us_leg", quote_set.us_leg, us_budget),
        ("usdttwd", quote_set.usdttwd, fx_budget),
    ):
        if not quote_is_fresh(quote, observed, budget):
            return None, f"stale_{name}"
    if tw_leg_book_quote_missing(quote_set.tw_leg):
        return None, "stale_tw_leg"
    if not quote_is_fresh(quote_set.tw_leg, observed, tw_leg_book_stale_seconds):
        return None, "stale_tw_leg"

    us_leg_price = book_price(quote_set.us_leg, us_leg_side)
    # A reclassified FX quote is a single reference rate: no book exists, so both
    # directions price it at that rate rather than failing on the absent bid/ask.
    usdttwd_price = (
        quote_set.usdttwd.price
        if fx_stale_seconds is not None
        else book_price(quote_set.usdttwd, usdttwd_side)
    )
    tw_leg_price = book_price(quote_set.tw_leg, tw_leg_side)
    if us_leg_price is None or usdttwd_price is None or tw_leg_price is None:
        return None, "missing_book"

    us_leg_twd_fair = us_leg_twd_fair_price(
        us_leg_price,
        usdttwd_price,
        adr_share_ratio,
    )
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


def us_leg_twd_fair_price(
    us_leg_price: float,
    usdttwd_price: float,
    adr_share_ratio: float,
) -> float:
    """Convert one ADR's foreign price into the TWD price of one ordinary share.

    ``adr_share_ratio`` is how many ordinary shares one ADR represents, so it is
    what makes the US leg comparable to a futures contract on the local stock.
    It is a per-pair fact and belongs to UsLegConfig; this function exists so the
    conversion has exactly one home. It previously appeared as a literal 5.0 in
    six places while position sizing read the configured value, which was correct
    only for as long as every pair happened to use 5.0 -- the same split-brain
    the plan's §10 warns about for contract_multiplier.
    """
    if adr_share_ratio <= 0:
        raise ValueError(f"adr_share_ratio must be positive, got {adr_share_ratio!r}")
    return us_leg_price * usdttwd_price / adr_share_ratio


def spread_from_prices(us_leg_twd_fair: float, tw_leg_price: float) -> float:
    return (us_leg_twd_fair - tw_leg_price) / (us_leg_twd_fair + tw_leg_price) * 200.0
