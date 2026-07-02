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
        qff=LiveQuote(
            source="fubon",
            symbol="QFFG6",
            timestamp=timestamp,
            price=100.0,
            bid=99.0,
            ask=101.0,
        ),
        tsm=LiveQuote(
            source="binance",
            symbol="TSM/USDT:USDT",
            timestamp=timestamp,
            price=20.5,
            bid=20.0,
            ask=21.0,
        ),
        usdttwd=LiveQuote(
            source="bitopro",
            symbol="USDT/TWD",
            timestamp=timestamp,
            price=30.5,
            bid=30.0,
            ask=31.0,
        ),
    )


def pair_plan(direction: Direction, timestamp: datetime):
    if direction == Direction.SHORT_TSM_LONG_QFF:
        tsm_side = OrderSide.SELL
        qff_side = OrderSide.BUY
    else:
        tsm_side = OrderSide.BUY
        qff_side = OrderSide.SELL
    return pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=direction,
        requests=(
            OrderRequest(
                broker=BrokerName.BINANCE_TSM,
                symbol="TSM/USDT:USDT",
                side=tsm_side,
                quantity=10.0,
                price=125.0,
                timestamp=timestamp,
                row_index=1,
            ),
            OrderRequest(
                broker=BrokerName.FUBON_QFF,
                symbol="QFFG6",
                side=qff_side,
                quantity=1.0,
                price=100.0,
                timestamp=timestamp,
                row_index=1,
                qff_symbol="QFFG6",
            ),
        ),
        reason="test",
    )


def leg_by_broker(plan, broker: BrokerName):
    return next(leg for leg in plan.legs if leg.broker == broker)


def test_short_entry_price_policy_uses_sell_bid_and_buy_ask() -> None:
    timestamp = ts("2026-06-22T09:00:00+08:00")
    plan = apply_live_touch_market_price_policy(
        pair_plan(Direction.SHORT_TSM_LONG_QFF, timestamp),
        quote_set(timestamp),
        max_plan_age_seconds=120,
    )

    tsm = leg_by_broker(plan, BrokerName.BINANCE_TSM)
    qff = leg_by_broker(plan, BrokerName.FUBON_QFF)
    assert plan.price_policy == LIVE_TOUCH_MARKET_PRICE_POLICY
    assert plan.order_type == "market"
    assert plan.max_plan_age_seconds == 120
    assert plan.plan_age_seconds == 0.0
    assert tsm.expected_price == 600.0
    assert tsm.price == 600.0
    assert tsm.trigger_bid == 600.0
    assert tsm.trigger_ask == 651.0
    assert tsm.raw["accounting_price"] == 125.0
    assert tsm.raw["tsm_contract_multiplier"] == 5.0
    assert qff.expected_price == 101.0
    assert qff.price == 101.0
    assert qff.trigger_bid == 99.0
    assert qff.trigger_ask == 101.0


def test_long_entry_price_policy_uses_buy_ask_and_sell_bid() -> None:
    timestamp = ts("2026-06-22T09:00:00+08:00")
    plan = apply_live_touch_market_price_policy(
        pair_plan(Direction.LONG_TSM_SHORT_QFF, timestamp),
        quote_set(timestamp),
        max_plan_age_seconds=120,
    )

    tsm = leg_by_broker(plan, BrokerName.BINANCE_TSM)
    qff = leg_by_broker(plan, BrokerName.FUBON_QFF)
    assert tsm.expected_price == 651.0
    assert tsm.price == 651.0
    assert qff.expected_price == 99.0
    assert qff.price == 99.0


def test_price_policy_plan_validates_and_simulated_fill_uses_expected_price() -> None:
    timestamp = ts("2026-06-22T09:00:00+08:00")
    plan = apply_live_touch_market_price_policy(
        pair_plan(Direction.SHORT_TSM_LONG_QFF, timestamp),
        quote_set(timestamp),
        max_plan_age_seconds=120,
    )

    validated = validate_pair_execution_plan(plan)
    outcome = SimulatedExecutionAdapter().execute(validated)

    assert validated.status == ExecutionPlanStatus.VALIDATED
    assert {fill.price for fill in outcome.fills} == {600.0, 101.0}
