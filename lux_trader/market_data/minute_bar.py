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


class LiveMinuteBarBuilder:
    def __init__(
        self,
        *,
        stale_seconds: float,
        max_leg_timestamp_skew_seconds: float,
        closed_dates: Iterable[date] = (),
        weekend_policy: str = DEFAULT_WEEKEND_POLICY,
    ) -> None:
        self.stale_seconds = stale_seconds
        self.max_leg_timestamp_skew_seconds = max_leg_timestamp_skew_seconds
        self.closed_dates = tuple(closed_dates)
        self.weekend_policy = validate_weekend_policy(weekend_policy)
        self.current_minute: datetime | None = None
        self.current_quotes: dict[str, LiveQuote] = {}
        self.last_ccf_close: float | None = None

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
        for name, quote in (("umc", umc), ("usd_twd", usd_twd)):
            age = abs((close_time - ensure_taipei(quote.timestamp)).total_seconds())
            if age > self.stale_seconds:
                return MinuteBuildResult(
                    None,
                    "market_data_stale",
                    {"source": name, "age_seconds": age},
                    quote_set,
                )

        ccf_is_fresh = False
        if ccf is not None:
            ccf_age = abs(
                (close_time - ensure_taipei(ccf.timestamp)).total_seconds()
            )
            ccf_is_fresh = ccf_age <= self.stale_seconds

        skew_quotes = [umc, usd_twd]
        if ccf is not None and ccf_is_fresh:
            skew_quotes.append(ccf)
        timestamps = [ensure_taipei(quote.timestamp) for quote in skew_quotes]
        skew = (max(timestamps) - min(timestamps)).total_seconds()
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
        if self.last_ccf_close is None:
            return MinuteBuildResult(
                None,
                "missing_ccf_forward_fill",
                quote_set=quote_set,
            )

        umc_twd_fair = umc.price * usd_twd.price / 5.0
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
                    spread=spread,
                ),
                self.closed_dates,
                weekend_policy=self.weekend_policy,
            ),
            quote_set=quote_set,
        )

