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
    us_leg_contract_multiplier: float = 5.0,
) -> PairExecutionPlan:
    return replace(
        plan,
        legs=tuple(
            _apply_leg_price_policy(
                leg,
                quote_set,
                us_leg_contract_multiplier=us_leg_contract_multiplier,
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
    us_leg_contract_multiplier: float,
) -> ExecutionLeg:
    if leg.broker == BrokerName.BINANCE:
        return _apply_binance_us_leg_price_policy(
            leg,
            quote_set,
            us_leg_contract_multiplier=us_leg_contract_multiplier,
        )
    if leg.broker == BrokerName.FUBON:
        return _apply_tw_leg_price_policy(leg, quote_set.tw_leg)
    return leg


def _apply_binance_us_leg_price_policy(
    leg: ExecutionLeg,
    quote_set: LiveQuoteSet,
    *,
    us_leg_contract_multiplier: float,
) -> ExecutionLeg:
    trigger_bid = _combined_us_leg_contract_twd_price(
        quote_set.us_leg.bid,
        quote_set.usdttwd.bid,
        us_leg_contract_multiplier,
    )
    trigger_ask = _combined_us_leg_contract_twd_price(
        quote_set.us_leg.ask,
        quote_set.usdttwd.ask,
        us_leg_contract_multiplier,
    )
    trigger_mid = _combined_us_leg_contract_twd_price(
        quote_set.us_leg.price,
        quote_set.usdttwd.price,
        us_leg_contract_multiplier,
    )
    expected = _side_expected_price(
        leg.side,
        bid=trigger_bid,
        ask=trigger_ask,
    )
    raw = {
        **(leg.raw or {}),
        "price_policy": LIVE_TOUCH_MARKET_PRICE_POLICY,
        "us_leg_bid": quote_set.us_leg.bid,
        "us_leg_ask": quote_set.us_leg.ask,
        "us_leg_price": quote_set.us_leg.price,
        "us_leg_timestamp": quote_set.us_leg.timestamp,
        "usdttwd_bid": quote_set.usdttwd.bid,
        "usdttwd_ask": quote_set.usdttwd.ask,
        "usdttwd_price": quote_set.usdttwd.price,
        "usdttwd_timestamp": quote_set.usdttwd.timestamp,
        "us_leg_contract_multiplier": us_leg_contract_multiplier,
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
        price_source="us_leg_usdttwd_top_of_book_twd_fair",
        raw=raw,
    )


def _apply_tw_leg_price_policy(leg: ExecutionLeg, quote: LiveQuote) -> ExecutionLeg:
    expected = _side_expected_price(
        leg.side,
        bid=quote.bid,
        ask=quote.ask,
    )
    raw = {
        **(leg.raw or {}),
        "price_policy": LIVE_TOUCH_MARKET_PRICE_POLICY,
        "tw_leg_bid": quote.bid,
        "tw_leg_ask": quote.ask,
        "tw_leg_price": quote.price,
        "tw_leg_timestamp": quote.timestamp,
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
        price_source="tw_leg_top_of_book",
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


def _combined_us_leg_twd_price(
    us_leg_price: float | None,
    usdttwd_price: float | None,
) -> float | None:
    if us_leg_price is None or usdttwd_price is None:
        return None
    return us_leg_price * usdttwd_price / 5.0


def _combined_us_leg_contract_twd_price(
    us_leg_price: float | None,
    usdttwd_price: float | None,
    multiplier: float,
) -> float | None:
    us_leg_twd_price = _combined_us_leg_twd_price(us_leg_price, usdttwd_price)
    if us_leg_twd_price is None:
        return None
    return us_leg_twd_price * float(multiplier)
