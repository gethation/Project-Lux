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
    umc_symbol: str,
    ccf_symbol: str,
    ccf_contract_multiplier: float,
) -> PositionSizing:
    fill_rows = tuple(fills)
    umc_fills = _matching_fills(
        fill_rows,
        broker=BrokerName.IBKR_UMC,
        symbol=umc_symbol,
    )
    ccf_fills = _matching_fills(
        fill_rows,
        broker=BrokerName.FUBON_CCF,
        symbol=ccf_symbol,
    )
    if not umc_fills:
        raise ExecutedPositionError("missing IBKR UMC fill")
    if not ccf_fills:
        raise ExecutedPositionError("missing Fubon CCF fill")

    umc_units = _signed_quantity(umc_fills)
    raw_ccf_contracts = _signed_quantity(ccf_fills)
    rounded_ccf_contracts = round(raw_ccf_contracts)
    if not isclose(raw_ccf_contracts, rounded_ccf_contracts, abs_tol=1e-9):
        raise ExecutedPositionError(
            f"Fubon CCF fill quantity must be integer lots: {raw_ccf_contracts}"
        )
    ccf_contracts = int(rounded_ccf_contracts)

    _validate_direction(direction, umc_units, ccf_contracts)
    multiplier = float(ccf_contract_multiplier)
    if not isfinite(multiplier) or multiplier <= 0:
        raise ExecutedPositionError("CCF contract multiplier must be positive")

    ccf_vwap = _volume_weighted_price(ccf_fills)
    return PositionSizing(
        umc_units=umc_units,
        ccf_units=ccf_contracts * multiplier,
        ccf_contracts=ccf_contracts,
        raw_ccf_contracts=abs(raw_ccf_contracts),
        actual_leg_notional_twd=abs(ccf_contracts) * multiplier * ccf_vwap,
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
    umc_units: float,
    ccf_contracts: int,
) -> None:
    if direction == Direction.SHORT_UMC_LONG_CCF:
        valid = umc_units < 0 and ccf_contracts > 0
    else:
        valid = umc_units > 0 and ccf_contracts < 0
    if not valid:
        raise ExecutedPositionError(
            "executed fill sides do not match the strategy direction"
        )
