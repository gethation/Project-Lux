from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta

from ..core.calendar import (
    WEEKEND_POLICY_FLAT,
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
        weekend_policy: str = WEEKEND_POLICY_FLAT,
        fx_stale_seconds: float | None = None,
    ) -> None:
        self.stale_seconds = stale_seconds
        self.max_leg_timestamp_skew_seconds = max_leg_timestamp_skew_seconds
        self.closed_dates = tuple(closed_dates)
        self.weekend_policy = validate_weekend_policy(weekend_policy)
        # None keeps the FX quote a co-timed leg: global staleness budget, and it
        # counts towards the leg-skew check. A value reclassifies it as a slow
        # reference rate with its own budget and no skew participation. See
        # FxConfig.stale_seconds for why those two go together.
        self.fx_stale_seconds = (
            None if fx_stale_seconds is None else float(fx_stale_seconds)
        )
        self.current_minute: datetime | None = None
        self.current_quotes: dict[str, LiveQuote] = {}
        self.last_tw_leg_close: float | None = None

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
        self.current_quotes["tw_leg"] = quote_set.tw_leg
        self.current_quotes["us_leg"] = quote_set.us_leg
        self.current_quotes["usdttwd"] = quote_set.usdttwd

    def _finalize_current_minute(self) -> MinuteBuildResult:
        if self.current_minute is None:
            return MinuteBuildResult(None, "no_current_minute")

        us_leg = self.current_quotes.get("us_leg")
        usdttwd = self.current_quotes.get("usdttwd")
        tw_leg = self.current_quotes.get("tw_leg")
        if us_leg is None or usdttwd is None:
            return MinuteBuildResult(
                None,
                "missing_required_quote",
                {"minute": self.current_minute.isoformat()},
            )
        quote_set = (
            LiveQuoteSet(tw_leg=tw_leg, us_leg=us_leg, usdttwd=usdttwd)
            if tw_leg is not None
            else None
        )

        close_time = self.current_minute + timedelta(minutes=1)
        fx_budget = (
            self.stale_seconds if self.fx_stale_seconds is None else self.fx_stale_seconds
        )
        for name, quote, budget in (
            ("us_leg", us_leg, self.stale_seconds),
            ("usdttwd", usdttwd, fx_budget),
        ):
            age = abs((close_time - ensure_taipei(quote.timestamp)).total_seconds())
            if age > budget:
                return MinuteBuildResult(
                    None,
                    "market_data_stale",
                    {"source": name, "age_seconds": age, "budget_seconds": budget},
                    quote_set,
                )

        tw_leg_is_fresh = False
        if tw_leg is not None:
            tw_leg_age = abs(
                (close_time - ensure_taipei(tw_leg.timestamp)).total_seconds()
            )
            tw_leg_is_fresh = tw_leg_age <= self.stale_seconds

        # A slow reference rate is always the oldest timestamp present, so leaving
        # it in would turn this check into a restatement of its own staleness
        # budget and stop it detecting what it exists for: two tick legs drifting
        # apart.
        skew_quotes = [us_leg] if self.fx_stale_seconds is not None else [us_leg, usdttwd]
        if tw_leg is not None and tw_leg_is_fresh:
            skew_quotes.append(tw_leg)
        timestamps = [ensure_taipei(quote.timestamp) for quote in skew_quotes]
        skew = (max(timestamps) - min(timestamps)).total_seconds()
        if skew > self.max_leg_timestamp_skew_seconds:
            return MinuteBuildResult(
                None,
                "leg_timestamp_skew",
                {"skew_seconds": skew},
                quote_set,
            )

        tw_leg_close = tw_leg.price if tw_leg is not None and tw_leg_is_fresh else None
        if tw_leg_close is not None:
            self.last_tw_leg_close = tw_leg_close
        if self.last_tw_leg_close is None:
            return MinuteBuildResult(
                None,
                "missing_tw_leg_forward_fill",
                quote_set=quote_set,
            )

        us_leg_twd_fair = us_leg.price * usdttwd.price / 5.0
        spread = (
            (us_leg_twd_fair - self.last_tw_leg_close)
            / (us_leg_twd_fair + self.last_tw_leg_close)
            * 200.0
        )
        return MinuteBuildResult(
            annotate_live_bar_with_closed_dates(
                MarketBar(
                    row_index=-1,
                    timestamp=self.current_minute,
                    tw_leg_close=tw_leg_close,
                    tw_leg_close_filled=self.last_tw_leg_close,
                    us_leg_twd_fair=us_leg_twd_fair,
                    spread=spread,
                ),
                self.closed_dates,
                weekend_policy=self.weekend_policy,
            ),
            quote_set=quote_set,
        )

