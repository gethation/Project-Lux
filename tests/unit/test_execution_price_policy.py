from __future__ import annotations

from datetime import datetime

from lux_trader.execution.intent import (
    ExecutionPlanType,
    ExecutionPlanStatus,
    pair_execution_plan_from_order_requests,
    validate_pair_execution_plan,
)
from lux_trader.execution.price_policy import (
    LIVE_TOUCH_MARKET_PRICE_POLICY,
    apply_live_touch_market_price_policy,
)
from lux_trader.execution import SimulatedExecutionAdapter
from lux_trader.market_data import LiveQuote, LiveQuoteSet
from lux_trader.core.models import BrokerName, Direction, OrderRequest, OrderSide


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def quote_set(timestamp: datetime) -> LiveQuoteSet:
    return LiveQuoteSet(
        ccf=LiveQuote(
            source="fubon",
            symbol="CCFG6",
            timestamp=timestamp,
            price=100.0,
            bid=99.0,
            ask=101.0,
        ),
        umc=LiveQuote(
            source="ibkr",
            symbol="UMC",
            timestamp=timestamp,
            price=20.5,
            bid=20.0,
            ask=21.0,
        ),
        usd_twd=LiveQuote(
            source="twelvedata",
            symbol="USD/TWD",
            timestamp=timestamp,
            price=30.5,
            bid=30.0,
            ask=31.0,
        ),
    )


def pair_plan(direction: Direction, timestamp: datetime):
    if direction == Direction.SHORT_UMC_LONG_CCF:
        umc_side = OrderSide.SELL
        ccf_side = OrderSide.BUY
    else:
        umc_side = OrderSide.BUY
        ccf_side = OrderSide.SELL
    return pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=direction,
        requests=(
            OrderRequest(
                broker=BrokerName.IBKR_UMC,
                symbol="UMC",
                side=umc_side,
                quantity=10.0,
                price=125.0,
                timestamp=timestamp,
                row_index=1,
            ),
            OrderRequest(
                broker=BrokerName.FUBON_CCF,
                symbol="CCFG6",
                side=ccf_side,
                quantity=1.0,
                price=100.0,
                timestamp=timestamp,
                row_index=1,
                ccf_symbol="CCFG6",
            ),
        ),
        reason="test",
    )


def leg_by_broker(plan, broker: BrokerName):
    return next(leg for leg in plan.legs if leg.broker == broker)


def test_short_entry_price_policy_uses_sell_bid_and_buy_ask() -> None:
    timestamp = ts("2026-06-22T09:00:00+08:00")
    plan = apply_live_touch_market_price_policy(
        pair_plan(Direction.SHORT_UMC_LONG_CCF, timestamp),
        quote_set(timestamp),
        max_plan_age_seconds=120,
    )

    umc = leg_by_broker(plan, BrokerName.IBKR_UMC)
    ccf = leg_by_broker(plan, BrokerName.FUBON_CCF)
    assert plan.price_policy == LIVE_TOUCH_MARKET_PRICE_POLICY
    assert plan.order_type == "market"
    assert plan.max_plan_age_seconds == 120
    assert plan.plan_age_seconds == 0.0
    assert umc.expected_price == 600.0
    assert umc.price == 600.0
    assert umc.trigger_bid == 600.0
    assert umc.trigger_ask == 651.0
    assert umc.raw["accounting_price"] == 125.0
    assert umc.raw["umc_contract_multiplier"] == 5.0
    assert ccf.expected_price == 101.0
    assert ccf.price == 101.0
    assert ccf.trigger_bid == 99.0
    assert ccf.trigger_ask == 101.0


def test_long_entry_price_policy_uses_buy_ask_and_sell_bid() -> None:
    timestamp = ts("2026-06-22T09:00:00+08:00")
    plan = apply_live_touch_market_price_policy(
        pair_plan(Direction.LONG_UMC_SHORT_CCF, timestamp),
        quote_set(timestamp),
        max_plan_age_seconds=120,
    )

    umc = leg_by_broker(plan, BrokerName.IBKR_UMC)
    ccf = leg_by_broker(plan, BrokerName.FUBON_CCF)
    assert umc.expected_price == 651.0
    assert umc.price == 651.0
    assert ccf.expected_price == 99.0
    assert ccf.price == 99.0


def test_price_policy_plan_validates_and_simulated_fill_uses_expected_price() -> None:
    timestamp = ts("2026-06-22T09:00:00+08:00")
    plan = apply_live_touch_market_price_policy(
        pair_plan(Direction.SHORT_UMC_LONG_CCF, timestamp),
        quote_set(timestamp),
        max_plan_age_seconds=120,
    )

    validated = validate_pair_execution_plan(plan)
    outcome = SimulatedExecutionAdapter().execute(validated)

    assert validated.status == ExecutionPlanStatus.VALIDATED
    assert {fill.price for fill in outcome.fills} == {600.0, 101.0}
