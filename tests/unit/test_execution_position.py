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


def test_position_sizing_uses_actual_signed_fill_quantities_and_qff_vwap() -> None:
    sizing = position_sizing_from_fills(
        Direction.SHORT_TSM_LONG_QFF,
        (
            fill(
                BrokerName.BINANCE_TSM,
                TSM_SYMBOL,
                OrderSide.SELL,
                909.0,
                1100.0,
                1,
            ),
            fill(
                BrokerName.FUBON_QFF,
                QFF_SYMBOL,
                OrderSide.BUY,
                4.0,
                999.0,
                2,
            ),
            fill(
                BrokerName.FUBON_QFF,
                QFF_SYMBOL,
                OrderSide.BUY,
                6.0,
                1001.0,
                3,
            ),
        ),
        tsm_symbol=TSM_SYMBOL,
        qff_symbol=QFF_SYMBOL,
        qff_contract_multiplier=100.0,
    )

    assert sizing.tsm_units == -909.0
    assert sizing.qff_contracts == 10
    assert sizing.qff_units == 1000.0
    assert sizing.raw_qff_contracts == 10.0
    assert sizing.actual_leg_notional_twd == pytest.approx(1_000_200.0)


@pytest.mark.parametrize(
    ("direction", "tsm_side", "qff_side"),
    [
        (
            Direction.SHORT_TSM_LONG_QFF,
            OrderSide.BUY,
            OrderSide.BUY,
        ),
        (
            Direction.LONG_TSM_SHORT_QFF,
            OrderSide.BUY,
            OrderSide.BUY,
        ),
    ],
)
def test_position_sizing_rejects_fill_sides_that_do_not_match_direction(
    direction: Direction,
    tsm_side: OrderSide,
    qff_side: OrderSide,
) -> None:
    with pytest.raises(ExecutedPositionError, match="strategy direction"):
        position_sizing_from_fills(
            direction,
            (
                fill(
                    BrokerName.BINANCE_TSM,
                    TSM_SYMBOL,
                    tsm_side,
                    10.0,
                    100.0,
                    1,
                ),
                fill(
                    BrokerName.FUBON_QFF,
                    QFF_SYMBOL,
                    qff_side,
                    1.0,
                    1000.0,
                    2,
                ),
            ),
            tsm_symbol=TSM_SYMBOL,
            qff_symbol=QFF_SYMBOL,
            qff_contract_multiplier=100.0,
        )


def test_position_sizing_rejects_missing_leg_and_fractional_qff_lot() -> None:
    tsm_fill = fill(
        BrokerName.BINANCE_TSM,
        TSM_SYMBOL,
        OrderSide.SELL,
        10.0,
        100.0,
        1,
    )
    with pytest.raises(ExecutedPositionError, match="missing Fubon"):
        position_sizing_from_fills(
            Direction.SHORT_TSM_LONG_QFF,
            (tsm_fill,),
            tsm_symbol=TSM_SYMBOL,
            qff_symbol=QFF_SYMBOL,
            qff_contract_multiplier=100.0,
        )

    with pytest.raises(ExecutedPositionError, match="integer lots"):
        position_sizing_from_fills(
            Direction.SHORT_TSM_LONG_QFF,
            (
                tsm_fill,
                fill(
                    BrokerName.FUBON_QFF,
                    QFF_SYMBOL,
                    OrderSide.BUY,
                    1.5,
                    1000.0,
                    2,
                ),
            ),
            tsm_symbol=TSM_SYMBOL,
            qff_symbol=QFF_SYMBOL,
            qff_contract_multiplier=100.0,
        )
