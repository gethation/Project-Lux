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
    us_leg_price: float,
    tw_leg_price: float,
    strategy: StrategyConfig,
    fees: FeeConfig,
) -> PositionSizing | None:
    if strategy.tw_leg_lots is not None:
        raw_tw_leg_contracts = float(strategy.tw_leg_lots)
        tw_leg_contract_count = strategy.tw_leg_lots
    else:
        raw_tw_leg_contracts = strategy.leg_notional_twd / (
            tw_leg_price * fees.tw_leg_contract_multiplier
        )
        tw_leg_contract_count = round_half_up_nonnegative(raw_tw_leg_contracts)
    if tw_leg_contract_count == 0:
        return None

    actual_leg_notional_twd = (
        tw_leg_contract_count * fees.tw_leg_contract_multiplier * tw_leg_price
    )
    us_leg_units = actual_leg_notional_twd / us_leg_contract_twd_price(us_leg_price, fees)
    tw_leg_units = tw_leg_contract_count * fees.tw_leg_contract_multiplier

    if direction == Direction.SHORT_US_LONG_TW:
        return PositionSizing(
            us_leg_units=-us_leg_units,
            tw_leg_units=tw_leg_units,
            tw_leg_contracts=tw_leg_contract_count,
            raw_tw_leg_contracts=raw_tw_leg_contracts,
            actual_leg_notional_twd=actual_leg_notional_twd,
        )
    return PositionSizing(
        us_leg_units=us_leg_units,
        tw_leg_units=-tw_leg_units,
        tw_leg_contracts=-tw_leg_contract_count,
        raw_tw_leg_contracts=raw_tw_leg_contracts,
        actual_leg_notional_twd=actual_leg_notional_twd,
    )


def us_leg_contract_twd_price(us_leg_twd_fair: float, fees: FeeConfig) -> float:
    multiplier = float(fees.us_leg_contract_multiplier)
    if multiplier <= 0:
        raise ValueError(
            f"Expected a positive USD-leg contract multiplier, got {multiplier}"
        )
    return us_leg_twd_fair * multiplier
