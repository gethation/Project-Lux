"""Venue-dispatched provider construction, shared by bootstrap and warmup.

Lives in its own module because both the live bootstrap and the warmup runner
need these builders, and warmup importing bootstrap would be a cycle.
"""

from __future__ import annotations

from lux_trader.config import AppConfig
from lux_trader.integrations.binance.market_data import BinanceMarketData
from lux_trader.integrations.bitopro.market_data import BitoProMarketData
from lux_trader.integrations.twelvedata import TwelveDataMarketData
from lux_trader.market_data import QuoteProvider
from lux_trader.market_data.cached_quote import CachedQuoteProvider


def build_us_leg_provider(config: AppConfig) -> QuoteProvider:
    """Pick the US-leg source this pair's config names.

    binance serves the TSM perpetual; ibkr serves the UMC cash equity. The IBKR
    provider is one object for both quotes and warmup history, so the whole
    pair runs over a single Gateway connection.
    """
    venue = config.active_pair.us_leg.venue
    if venue == "binance":
        return BinanceMarketData()
    if venue == "ibkr":
        from lux_trader.integrations.ibkr.market_data import IbkrUmcQuoteProvider

        return IbkrUmcQuoteProvider()
    raise RuntimeError(
        f"Pair {config.active_pair.id!r} names an unknown us_leg venue {venue!r}; "
        "supported: binance, ibkr"
    )


def build_fx_provider(config: AppConfig) -> QuoteProvider:
    """Pick the FX source this pair's config names.

    QFF/TSM prices its US leg in USDT, so BitoPro's USDT/TWD is the correct rate
    for it. CCF/UMC prices UMC in dollars and needs real USD/TWD -- §8.2 measures
    BitoPro's Taiwan-local crypto premium at 77% of that spread's own standard
    deviation, which a rolling z-score does not absorb.

    The Twelve Data source is metered (8 credits/minute on the free tier) and is
    therefore always wrapped in the TTL cache; historical warmup calls pass
    through the cache to the raw provider.
    """
    fx = config.active_pair.fx
    if fx.venue == "bitopro":
        # A tick source, not metered, so it is used unwrapped.
        return BitoProMarketData()
    if fx.venue == "twelvedata":
        return CachedQuoteProvider(
            TwelveDataMarketData(),
            ttl_seconds=fx.cache_ttl_seconds,
            max_serve_seconds=fx.max_serve_seconds,
        )
    raise RuntimeError(
        f"Pair {config.active_pair.id!r} names an unknown fx venue {fx.venue!r}; "
        "supported: bitopro, twelvedata"
    )
