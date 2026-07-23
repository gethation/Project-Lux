from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import (
    BrokerName,
    Direction,
    Fill,
    OrderSide,
)
from lux_trader.execution.position import (
    ExecutedPositionError,
    position_sizing_from_fills,
)


TSM_SYMBOL = "TSM/USDT:USDT"
QFF_SYMBOL = "QFFG6"


def ts() -> datetime:
    return datetime.fromisoformat("2026-06-26T09:15:00+08:00")


def fill(
    broker: BrokerName,
    symbol: str,
    side: OrderSide,
    quantity: float,
    price: float,
    index: int,
) -> Fill:
    return Fill(
        fill_id=f"FILL-{index}",
        order_id=f"ORDER-{index}",
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        fee_twd=0.0,
        timestamp=ts(),
        row_index=1,
    )


def test_position_sizing_uses_actual_signed_fill_quantities_and_tw_leg_vwap() -> None:
    sizing = position_sizing_from_fills(
        Direction.SHORT_US_LONG_TW,
        (
            fill(
                BrokerName.BINANCE,
                TSM_SYMBOL,
                OrderSide.SELL,
                909.0,
                1100.0,
                1,
            ),
            fill(
                BrokerName.FUBON,
                QFF_SYMBOL,
                OrderSide.BUY,
                4.0,
                999.0,
                2,
            ),
            fill(
                BrokerName.FUBON,
                QFF_SYMBOL,
                OrderSide.BUY,
                6.0,
                1001.0,
                3,
            ),
        ),
        us_leg_symbol=TSM_SYMBOL,
        tw_leg_symbol=QFF_SYMBOL,
        tw_leg_contract_multiplier=100.0,
    )

    assert sizing.us_leg_units == -909.0
    assert sizing.tw_leg_contracts == 10
    assert sizing.tw_leg_units == 1000.0
    assert sizing.raw_tw_leg_contracts == 10.0
    assert sizing.actual_leg_notional_twd == pytest.approx(1_000_200.0)


@pytest.mark.parametrize(
    ("direction", "us_leg_side", "tw_leg_side"),
    [
        (
            Direction.SHORT_US_LONG_TW,
            OrderSide.BUY,
            OrderSide.BUY,
        ),
        (
            Direction.LONG_US_SHORT_TW,
            OrderSide.BUY,
            OrderSide.BUY,
        ),
    ],
)
def test_position_sizing_rejects_fill_sides_that_do_not_match_direction(
    direction: Direction,
    us_leg_side: OrderSide,
    tw_leg_side: OrderSide,
) -> None:
    with pytest.raises(ExecutedPositionError, match="strategy direction"):
        position_sizing_from_fills(
            direction,
            (
                fill(
                    BrokerName.BINANCE,
                    TSM_SYMBOL,
                    us_leg_side,
                    10.0,
                    100.0,
                    1,
                ),
                fill(
                    BrokerName.FUBON,
                    QFF_SYMBOL,
                    tw_leg_side,
                    1.0,
                    1000.0,
                    2,
                ),
            ),
            us_leg_symbol=TSM_SYMBOL,
            tw_leg_symbol=QFF_SYMBOL,
            tw_leg_contract_multiplier=100.0,
        )


def test_position_sizing_rejects_missing_leg_and_fractional_tw_leg_lot() -> None:
    us_leg_fill = fill(
        BrokerName.BINANCE,
        TSM_SYMBOL,
        OrderSide.SELL,
        10.0,
        100.0,
        1,
    )
    with pytest.raises(ExecutedPositionError, match="missing Fubon"):
        position_sizing_from_fills(
            Direction.SHORT_US_LONG_TW,
            (us_leg_fill,),
            us_leg_symbol=TSM_SYMBOL,
            tw_leg_symbol=QFF_SYMBOL,
            tw_leg_contract_multiplier=100.0,
        )

    with pytest.raises(ExecutedPositionError, match="integer lots"):
        position_sizing_from_fills(
            Direction.SHORT_US_LONG_TW,
            (
                us_leg_fill,
                fill(
                    BrokerName.FUBON,
                    QFF_SYMBOL,
                    OrderSide.BUY,
                    1.5,
                    1000.0,
                    2,
                ),
            ),
            us_leg_symbol=TSM_SYMBOL,
            tw_leg_symbol=QFF_SYMBOL,
            tw_leg_contract_multiplier=100.0,
        )
