from __future__ import annotations

from collections.abc import Iterable
from math import isclose, isfinite

from ..core.models import (
    BrokerName,
    Direction,
    Fill,
    OrderSide,
    PositionSizing,
)


class ExecutedPositionError(ValueError):
    """Raised when successful execution fills cannot form a valid pair position."""


def position_sizing_from_fills(
    direction: Direction,
    fills: Iterable[Fill],
    *,
    tsm_symbol: str,
    qff_symbol: str,
    qff_contract_multiplier: float,
) -> PositionSizing:
    fill_rows = tuple(fills)
    tsm_fills = _matching_fills(
        fill_rows,
        broker=BrokerName.BINANCE_TSM,
        symbol=tsm_symbol,
    )
    qff_fills = _matching_fills(
        fill_rows,
        broker=BrokerName.FUBON_QFF,
        symbol=qff_symbol,
    )
    if not tsm_fills:
        raise ExecutedPositionError("missing Binance TSM fill")
    if not qff_fills:
        raise ExecutedPositionError("missing Fubon QFF fill")

    tsm_units = _signed_quantity(tsm_fills)
    raw_qff_contracts = _signed_quantity(qff_fills)
    rounded_qff_contracts = round(raw_qff_contracts)
    if not isclose(raw_qff_contracts, rounded_qff_contracts, abs_tol=1e-9):
        raise ExecutedPositionError(
            f"Fubon QFF fill quantity must be integer lots: {raw_qff_contracts}"
        )
    qff_contracts = int(rounded_qff_contracts)

    _validate_direction(direction, tsm_units, qff_contracts)
    multiplier = float(qff_contract_multiplier)
    if not isfinite(multiplier) or multiplier <= 0:
        raise ExecutedPositionError("QFF contract multiplier must be positive")

    qff_vwap = _volume_weighted_price(qff_fills)
    return PositionSizing(
        tsm_units=tsm_units,
        qff_units=qff_contracts * multiplier,
        qff_contracts=qff_contracts,
        raw_qff_contracts=abs(raw_qff_contracts),
        actual_leg_notional_twd=abs(qff_contracts) * multiplier * qff_vwap,
    )


def _matching_fills(
    fills: Iterable[Fill],
    *,
    broker: BrokerName,
    symbol: str,
) -> tuple[Fill, ...]:
    return tuple(
        fill
        for fill in fills
        if fill.broker == broker and fill.symbol == symbol
    )


def _signed_quantity(fills: Iterable[Fill]) -> float:
    quantity = sum(
        float(fill.quantity)
        * (1.0 if fill.side == OrderSide.BUY else -1.0)
        for fill in fills
    )
    if not isfinite(quantity) or isclose(quantity, 0.0, abs_tol=1e-12):
        raise ExecutedPositionError("net executed quantity must be non-zero")
    return quantity


def _volume_weighted_price(fills: Iterable[Fill]) -> float:
    rows = tuple(fills)
    total_quantity = sum(abs(float(fill.quantity)) for fill in rows)
    if total_quantity <= 0:
        raise ExecutedPositionError("executed quantity must be positive")
    weighted_value = 0.0
    for fill in rows:
        price = float(fill.price)
        if not isfinite(price) or price <= 0:
            raise ExecutedPositionError("executed fill price must be positive")
        weighted_value += abs(float(fill.quantity)) * price
    return weighted_value / total_quantity


def _validate_direction(
    direction: Direction,
    tsm_units: float,
    qff_contracts: int,
) -> None:
    if direction == Direction.SHORT_TSM_LONG_QFF:
        valid = tsm_units < 0 and qff_contracts > 0
    else:
        valid = tsm_units > 0 and qff_contracts < 0
    if not valid:
        raise ExecutedPositionError(
            "executed fill sides do not match the strategy direction"
        )
