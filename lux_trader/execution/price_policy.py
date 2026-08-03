from __future__ import annotations

from dataclasses import replace

from .intent import (
    ExecutionLeg,
    ExecutionOrderType,
    PairExecutionPlan,
)
from ..market_data.types import LiveQuote, LiveQuoteSet
from ..core.models import BrokerName, OrderSide


LIVE_TOUCH_MARKET_PRICE_POLICY = "live_touch_market"


def apply_live_touch_market_price_policy(
    plan: PairExecutionPlan,
    quote_set: LiveQuoteSet,
    *,
    max_plan_age_seconds: int | None = None,
    plan_age_seconds: float = 0.0,
    umc_contract_multiplier: float = 5.0,
) -> PairExecutionPlan:
    return replace(
        plan,
        legs=tuple(
            _apply_leg_price_policy(
                leg,
                quote_set,
                umc_contract_multiplier=umc_contract_multiplier,
            )
            for leg in plan.legs
        ),
        order_type=ExecutionOrderType.MARKET.value,
        price_policy=LIVE_TOUCH_MARKET_PRICE_POLICY,
        plan_age_seconds=plan_age_seconds,
        max_plan_age_seconds=max_plan_age_seconds,
    )


def _apply_leg_price_policy(
    leg: ExecutionLeg,
    quote_set: LiveQuoteSet,
    *,
    umc_contract_multiplier: float,
) -> ExecutionLeg:
    if leg.broker == BrokerName.IBKR_UMC:
        return _apply_umc_price_policy(
            leg,
            quote_set,
            umc_contract_multiplier=umc_contract_multiplier,
        )
    if leg.broker == BrokerName.FUBON_CCF:
        return _apply_ccf_price_policy(leg, quote_set.ccf)
    return leg


def _apply_umc_price_policy(
    leg: ExecutionLeg,
    quote_set: LiveQuoteSet,
    *,
    umc_contract_multiplier: float,
) -> ExecutionLeg:
    # One rate for all three legs of the conversion, deliberately.
    #
    # USD/TWD is a scalar that converts a price; it is not a book this pair
    # crosses. Twelve Data serves no book at all, so usd_twd.bid and .ask are
    # ALWAYS None -- which made trigger_bid and trigger_ask None while
    # trigger_mid worked, and the plan validator rejected every entry on
    # `expected_price_positive` and `trigger_book_present`. The pair could not
    # take a single trade, in dry-run or live.
    #
    # This is the same mistake Phase B already corrected in
    # core/tradable_spread.py, which is where the directional z-score gets its
    # prices; price_policy.py was missed. The directionality that matters comes
    # from UMC's own bid/ask, which is real and entitled -- see the FX staleness
    # note in market_data/minute_bar.py for the third face of the same idea.
    usd_twd_rate = quote_set.usd_twd.price
    trigger_bid = _combined_umc_contract_twd_price(
        quote_set.umc.bid,
        usd_twd_rate,
        umc_contract_multiplier,
    )
    trigger_ask = _combined_umc_contract_twd_price(
        quote_set.umc.ask,
        usd_twd_rate,
        umc_contract_multiplier,
    )
    trigger_mid = _combined_umc_contract_twd_price(
        quote_set.umc.price,
        usd_twd_rate,
        umc_contract_multiplier,
    )
    expected = _side_expected_price(
        leg.side,
        bid=trigger_bid,
        ask=trigger_ask,
    )
    raw = {
        **(leg.raw or {}),
        "price_policy": LIVE_TOUCH_MARKET_PRICE_POLICY,
        "umc_bid": quote_set.umc.bid,
        "umc_ask": quote_set.umc.ask,
        "umc_price": quote_set.umc.price,
        "umc_timestamp": quote_set.umc.timestamp,
        "usd_twd_bid": quote_set.usd_twd.bid,
        "usd_twd_ask": quote_set.usd_twd.ask,
        "usd_twd_price": quote_set.usd_twd.price,
        "usd_twd_timestamp": quote_set.usd_twd.timestamp,
        "umc_contract_multiplier": umc_contract_multiplier,
        "accounting_price": leg.price,
    }
    return replace(
        leg,
        price=expected if expected is not None else leg.price,
        order_type=ExecutionOrderType.MARKET.value,
        expected_price=expected,
        trigger_bid=trigger_bid,
        trigger_ask=trigger_ask,
        trigger_mid=trigger_mid,
        price_source="umc_usd_twd_top_of_book_twd_fair",
        raw=raw,
    )


def _apply_ccf_price_policy(leg: ExecutionLeg, quote: LiveQuote) -> ExecutionLeg:
    expected = _side_expected_price(
        leg.side,
        bid=quote.bid,
        ask=quote.ask,
    )
    raw = {
        **(leg.raw or {}),
        "price_policy": LIVE_TOUCH_MARKET_PRICE_POLICY,
        "ccf_bid": quote.bid,
        "ccf_ask": quote.ask,
        "ccf_price": quote.price,
        "ccf_timestamp": quote.timestamp,
        "accounting_price": leg.price,
    }
    return replace(
        leg,
        price=expected if expected is not None else leg.price,
        order_type=ExecutionOrderType.MARKET.value,
        expected_price=expected,
        trigger_bid=quote.bid,
        trigger_ask=quote.ask,
        trigger_mid=quote.price,
        price_source="ccf_top_of_book",
        raw=raw,
    )


def _side_expected_price(
    side: OrderSide,
    *,
    bid: float | None,
    ask: float | None,
) -> float | None:
    if side == OrderSide.BUY:
        return ask
    if side == OrderSide.SELL:
        return bid
    return None


def _combined_umc_twd_price(
    umc_price: float | None,
    usd_twd_price: float | None,
) -> float | None:
    if umc_price is None or usd_twd_price is None:
        return None
    return umc_price * usd_twd_price / 5.0


def _combined_umc_contract_twd_price(
    umc_price: float | None,
    usd_twd_price: float | None,
    multiplier: float,
) -> float | None:
    umc_twd_price = _combined_umc_twd_price(umc_price, usd_twd_price)
    if umc_twd_price is None:
        return None
    return umc_twd_price * float(multiplier)
