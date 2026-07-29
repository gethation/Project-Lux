from __future__ import annotations

import math

from ..config import FeeConfig, StrategyConfig
from .models import Direction, PositionSizing


def round_half_up_nonnegative(value: float) -> int:
    if value < 0:
        raise ValueError(f"Expected a non-negative value, got {value}")
    return int(math.floor(value + 0.5))


def size_position_for_direction(
    direction: Direction,
    umc_price: float,
    ccf_price: float,
    strategy: StrategyConfig,
    fees: FeeConfig,
) -> PositionSizing | None:
    if strategy.ccf_lots is not None:
        raw_ccf_contracts = float(strategy.ccf_lots)
        ccf_contract_count = strategy.ccf_lots
    else:
        raw_ccf_contracts = strategy.leg_notional_twd / (
            ccf_price * fees.ccf_contract_multiplier
        )
        ccf_contract_count = round_half_up_nonnegative(raw_ccf_contracts)
    if ccf_contract_count == 0:
        return None

    actual_leg_notional_twd = (
        ccf_contract_count * fees.ccf_contract_multiplier * ccf_price
    )
    umc_units = actual_leg_notional_twd / umc_contract_twd_price(umc_price, fees)
    ccf_units = ccf_contract_count * fees.ccf_contract_multiplier

    if direction == Direction.SHORT_UMC_LONG_CCF:
        return PositionSizing(
            umc_units=-umc_units,
            ccf_units=ccf_units,
            ccf_contracts=ccf_contract_count,
            raw_ccf_contracts=raw_ccf_contracts,
            actual_leg_notional_twd=actual_leg_notional_twd,
        )
    return PositionSizing(
        umc_units=umc_units,
        ccf_units=-ccf_units,
        ccf_contracts=-ccf_contract_count,
        raw_ccf_contracts=raw_ccf_contracts,
        actual_leg_notional_twd=actual_leg_notional_twd,
    )


def umc_contract_twd_price(umc_twd_fair: float, fees: FeeConfig) -> float:
    multiplier = float(fees.umc_contract_multiplier)
    if multiplier <= 0:
        raise ValueError(f"Expected a positive UMC contract multiplier, got {multiplier}")
    return umc_twd_fair * multiplier
