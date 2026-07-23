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
    us_leg_symbol: str,
    tw_leg_symbol: str,
    tw_leg_contract_multiplier: float,
) -> PositionSizing:
    fill_rows = tuple(fills)
    us_leg_fills = _matching_fills(
        fill_rows,
        broker=BrokerName.BINANCE,
        symbol=us_leg_symbol,
    )
    tw_leg_fills = _matching_fills(
        fill_rows,
        broker=BrokerName.FUBON,
        symbol=tw_leg_symbol,
    )
    if not us_leg_fills:
        raise ExecutedPositionError(f"missing Binance {us_leg_symbol} fill")
    if not tw_leg_fills:
        raise ExecutedPositionError(f"missing Fubon {tw_leg_symbol} fill")

    us_leg_units = _signed_quantity(us_leg_fills)
    raw_tw_leg_contracts = _signed_quantity(tw_leg_fills)
    rounded_tw_leg_contracts = round(raw_tw_leg_contracts)
    if not isclose(raw_tw_leg_contracts, rounded_tw_leg_contracts, abs_tol=1e-9):
        raise ExecutedPositionError(
            f"Fubon {tw_leg_symbol} fill quantity must be integer lots: "
            f"{raw_tw_leg_contracts}"
        )
    tw_leg_contracts = int(rounded_tw_leg_contracts)

    _validate_direction(direction, us_leg_units, tw_leg_contracts)
    multiplier = float(tw_leg_contract_multiplier)
    if not isfinite(multiplier) or multiplier <= 0:
        raise ExecutedPositionError(
            f"{tw_leg_symbol} contract multiplier must be positive"
        )

    tw_leg_vwap = _volume_weighted_price(tw_leg_fills)
    return PositionSizing(
        us_leg_units=us_leg_units,
        tw_leg_units=tw_leg_contracts * multiplier,
        tw_leg_contracts=tw_leg_contracts,
        raw_tw_leg_contracts=abs(raw_tw_leg_contracts),
        actual_leg_notional_twd=abs(tw_leg_contracts) * multiplier * tw_leg_vwap,
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
    us_leg_units: float,
    tw_leg_contracts: int,
) -> None:
    if direction == Direction.SHORT_US_LONG_TW:
        valid = us_leg_units < 0 and tw_leg_contracts > 0
    else:
        valid = us_leg_units > 0 and tw_leg_contracts < 0
    if not valid:
        raise ExecutedPositionError(
            "executed fill sides do not match the strategy direction"
        )
