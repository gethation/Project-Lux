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
    tsm_contract_multiplier: float = 5.0,
) -> PairExecutionPlan:
    return replace(
        plan,
        legs=tuple(
            _apply_leg_price_policy(
                leg,
                quote_set,
                tsm_contract_multiplier=tsm_contract_multiplier,
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
    tsm_contract_multiplier: float,
) -> ExecutionLeg:
    if leg.broker == BrokerName.BINANCE_TSM:
        return _apply_binance_tsm_price_policy(
            leg,
            quote_set,
            tsm_contract_multiplier=tsm_contract_multiplier,
        )
    if leg.broker == BrokerName.FUBON_QFF:
        return _apply_qff_price_policy(leg, quote_set.qff)
    return leg


def _apply_binance_tsm_price_policy(
    leg: ExecutionLeg,
    quote_set: LiveQuoteSet,
    *,
    tsm_contract_multiplier: float,
) -> ExecutionLeg:
    trigger_bid = _combined_tsm_contract_twd_price(
        quote_set.tsm.bid,
        quote_set.usdttwd.bid,
        tsm_contract_multiplier,
    )
    trigger_ask = _combined_tsm_contract_twd_price(
        quote_set.tsm.ask,
        quote_set.usdttwd.ask,
        tsm_contract_multiplier,
    )
    trigger_mid = _combined_tsm_contract_twd_price(
        quote_set.tsm.price,
        quote_set.usdttwd.price,
        tsm_contract_multiplier,
    )
    expected = _side_expected_price(
        leg.side,
        bid=trigger_bid,
        ask=trigger_ask,
    )
    raw = {
        **(leg.raw or {}),
        "price_policy": LIVE_TOUCH_MARKET_PRICE_POLICY,
        "tsm_bid": quote_set.tsm.bid,
        "tsm_ask": quote_set.tsm.ask,
        "tsm_price": quote_set.tsm.price,
        "tsm_timestamp": quote_set.tsm.timestamp,
        "usdttwd_bid": quote_set.usdttwd.bid,
        "usdttwd_ask": quote_set.usdttwd.ask,
        "usdttwd_price": quote_set.usdttwd.price,
        "usdttwd_timestamp": quote_set.usdttwd.timestamp,
        "tsm_contract_multiplier": tsm_contract_multiplier,
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
        price_source="tsm_usdttwd_top_of_book_twd_fair",
        raw=raw,
    )


def _apply_qff_price_policy(leg: ExecutionLeg, quote: LiveQuote) -> ExecutionLeg:
    expected = _side_expected_price(
        leg.side,
        bid=quote.bid,
        ask=quote.ask,
    )
    raw = {
        **(leg.raw or {}),
        "price_policy": LIVE_TOUCH_MARKET_PRICE_POLICY,
        "qff_bid": quote.bid,
        "qff_ask": quote.ask,
        "qff_price": quote.price,
        "qff_timestamp": quote.timestamp,
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
        price_source="qff_top_of_book",
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


def _combined_tsm_twd_price(
    tsm_price: float | None,
    usdttwd_price: float | None,
) -> float | None:
    if tsm_price is None or usdttwd_price is None:
        return None
    return tsm_price * usdttwd_price / 5.0


def _combined_tsm_contract_twd_price(
    tsm_price: float | None,
    usdttwd_price: float | None,
    multiplier: float,
) -> float | None:
    tsm_twd_price = _combined_tsm_twd_price(tsm_price, usdttwd_price)
    if tsm_twd_price is None:
        return None
    return tsm_twd_price * float(multiplier)
