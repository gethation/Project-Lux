from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from ..core.calendar import (
    DEFAULT_WEEKEND_POLICY,
    annotate_live_bar_with_closed_dates,
    validate_weekend_policy,
)
from ..core.models import MarketBar
from ..core.time import ensure_taipei
from .session import floor_minute
from .types import LiveQuote, LiveQuoteSet, MinuteBuildResult


def price_time(quote: LiveQuote) -> datetime:
    """When this quote's `price` was last true.

    Falls back to the quote timestamp for sources where they are the same
    thing -- CCF and FX both publish a price and a time together. Only the IBKR
    quote separates them, because its `price` is a trade and its timestamp is
    the book.
    """
    return quote.price_timestamp or quote.timestamp


class LiveMinuteBarBuilder:
    def __init__(
        self,
        *,
        stale_seconds: float,
        max_leg_timestamp_skew_seconds: float,
        usd_twd_stale_seconds: float | None = None,
        umc_trade_stale_seconds: float = 60.0,
        ccf_forward_fill_max_seconds: float | None = None,
        closed_dates: Iterable[date] = (),
        weekend_policy: str = DEFAULT_WEEKEND_POLICY,
    ) -> None:
        self.stale_seconds = stale_seconds
        # How old the last TRADE may be, separately from the book. One bar
        # period by default: the correct close of a quiet minute IS the last
        # trade in it, so anything tighter rejects healthy minutes, and anything
        # much looser lets a genuinely dead print set the bar.
        self.umc_trade_stale_seconds = float(umc_trade_stale_seconds)
        # USD/TWD is a reference rate, not a book this pair crosses, and it
        # arrives from a vendor cache rather than an exchange feed. Holding it
        # to the exchange budget rejects every single minute -- see the skew
        # note below for the other half of the same mistake.
        self.usd_twd_stale_seconds = (
            stale_seconds if usd_twd_stale_seconds is None else usd_twd_stale_seconds
        )
        # A CEILING on the forward fill, not a second freshness budget.
        # `stale_seconds` decides whether THIS minute's CCF quote is fresh
        # enough to be the close; when it is not, `last_ccf_close` carries the
        # previous one, which is correct for a thin minute and wrong for a dead
        # feed. Nothing used to bound the carry, so a websocket the peer closed
        # produced a bar every minute from a price hours old. None keeps the
        # old unbounded behaviour for callers that have not opted in.
        self.ccf_forward_fill_max_seconds = (
            None
            if ccf_forward_fill_max_seconds is None
            else float(ccf_forward_fill_max_seconds)
        )
        self.max_leg_timestamp_skew_seconds = max_leg_timestamp_skew_seconds
        self.closed_dates = tuple(closed_dates)
        self.weekend_policy = validate_weekend_policy(weekend_policy)
        self.current_minute: datetime | None = None
        self.current_quotes: dict[str, LiveQuote] = {}
        self.last_ccf_close: float | None = None
        # When `last_ccf_close` was last set from a FRESH quote. Seeded
        # alongside it at startup so a warmup-seeded price cannot be carried
        # into live trading unchecked.
        self.last_ccf_close_at: datetime | None = None

    def reset_current_minute(self) -> None:
        self.current_minute = None
        self.current_quotes = {}

    def update(
        self,
        quote_set: LiveQuoteSet,
        observed_at: datetime,
    ) -> MinuteBuildResult | None:
        observed_at = ensure_taipei(observed_at)
        minute = floor_minute(observed_at)
        if self.current_minute is None:
            self.current_minute = minute
            self._update_current_quotes(quote_set)
            return None

        if minute == self.current_minute:
            self._update_current_quotes(quote_set)
            return None

        result = self._finalize_current_minute()
        self.current_minute = minute
        self.current_quotes = {}
        self._update_current_quotes(quote_set)
        return result

    def _update_current_quotes(self, quote_set: LiveQuoteSet) -> None:
        self.current_quotes["ccf"] = quote_set.ccf
        self.current_quotes["umc"] = quote_set.umc
        self.current_quotes["usd_twd"] = quote_set.usd_twd

    def _finalize_current_minute(self) -> MinuteBuildResult:
        if self.current_minute is None:
            return MinuteBuildResult(None, "no_current_minute")

        umc = self.current_quotes.get("umc")
        usd_twd = self.current_quotes.get("usd_twd")
        ccf = self.current_quotes.get("ccf")
        if umc is None or usd_twd is None:
            return MinuteBuildResult(
                None,
                "missing_required_quote",
                {"minute": self.current_minute.isoformat()},
            )
        quote_set = (
            LiveQuoteSet(ccf=ccf, umc=umc, usd_twd=usd_twd)
            if ccf is not None
            else None
        )

        close_time = self.current_minute + timedelta(minutes=1)
        for name, quote, budget in (
            ("umc", umc, self.stale_seconds),
            ("usd_twd", usd_twd, self.usd_twd_stale_seconds),
        ):
            age = abs((close_time - ensure_taipei(quote.timestamp)).total_seconds())
            if age > budget:
                return MinuteBuildResult(
                    None,
                    "market_data_stale",
                    {"source": name, "age_seconds": age, "budget_seconds": budget},
                    quote_set,
                )

        ccf_is_fresh = False
        if ccf is not None:
            ccf_age = abs(
                (close_time - ensure_taipei(ccf.timestamp)).total_seconds()
            )
            ccf_is_fresh = ccf_age <= self.stale_seconds

        # The bar's close is a TRADE price (umc.price prefers `last`), and the
        # quote is stamped with the book clock, so the staleness check above
        # does not bound it. Without this, a thirty-second-old print reads as
        # zero seconds old and is laundered into the rolling mean and std.
        umc_price_age = abs(
            (close_time - ensure_taipei(price_time(umc))).total_seconds()
        )
        if umc_price_age > self.umc_trade_stale_seconds:
            return MinuteBuildResult(
                None,
                "stale_trade_price",
                {
                    "source": "umc",
                    "age_seconds": umc_price_age,
                    "budget_seconds": self.umc_trade_stale_seconds,
                },
                quote_set,
            )

        # Skew asks whether the two LEGS are priced from the same moment. Two
        # deliberate choices:
        #
        # USD/TWD is excluded. It is not a leg -- it only converts one of them
        # -- and it arrives on a vendor cadence, so including it reported a
        # healthy pair as skewed by exactly the cache age.
        #
        # It compares PRICE timestamps, not quote timestamps. Both are venue
        # clocks (CCF's exchange print, UMC's trade print), whereas UMC's quote
        # timestamp is a local receive time -- comparing that against an
        # exchange clock measures clock offset, not skew.
        #
        # With no fresh CCF there is no second leg, so there is nothing to
        # compare; say so rather than computing max-min over one element and
        # emerging with a gate that structurally cannot fire.
        if ccf is not None and ccf_is_fresh:
            skew = abs(
                (
                    ensure_taipei(price_time(umc)) - ensure_taipei(price_time(ccf))
                ).total_seconds()
            )
            if skew > self.max_leg_timestamp_skew_seconds:
                return MinuteBuildResult(
                    None,
                    "leg_timestamp_skew",
                    {"skew_seconds": skew},
                    quote_set,
                )

        ccf_close = ccf.price if ccf is not None and ccf_is_fresh else None
        if ccf_close is not None:
            self.last_ccf_close = ccf_close
            self.last_ccf_close_at = close_time
        if self.last_ccf_close is None:
            return MinuteBuildResult(
                None,
                "missing_ccf_forward_fill",
                quote_set=quote_set,
            )

        # The carry has a limit. Past it the CCF leg is not thin, it is gone,
        # and a spread built from a price this old is a fiction with a real
        # position behind it.
        if self.ccf_forward_fill_max_seconds is not None and ccf_close is None:
            carried_for = (
                None
                if self.last_ccf_close_at is None
                else (close_time - ensure_taipei(self.last_ccf_close_at)).total_seconds()
            )
            if carried_for is None or carried_for > self.ccf_forward_fill_max_seconds:
                return MinuteBuildResult(
                    None,
                    "market_data_stale",
                    {
                        "source": "ccf",
                        "age_seconds": carried_for,
                        "budget_seconds": self.ccf_forward_fill_max_seconds,
                        "forward_filled": True,
                    },
                    quote_set,
                )

        usd_twd_rate = usd_twd.price
        umc_twd_fair = umc.price * usd_twd_rate / 5.0
        spread = (
            (umc_twd_fair - self.last_ccf_close)
            / (umc_twd_fair + self.last_ccf_close)
            * 200.0
        )
        return MinuteBuildResult(
            annotate_live_bar_with_closed_dates(
                MarketBar(
                    row_index=-1,
                    timestamp=self.current_minute,
                    ccf_close=ccf_close,
                    ccf_close_filled=self.last_ccf_close,
                    umc_twd_fair=umc_twd_fair,
                    usd_twd=usd_twd_rate,
                    spread=spread,
                ),
                self.closed_dates,
                weekend_policy=self.weekend_policy,
            ),
            quote_set=quote_set,
        )

