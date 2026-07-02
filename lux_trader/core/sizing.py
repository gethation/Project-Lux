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
    tsm_price: float,
    qff_price: float,
    strategy: StrategyConfig,
    fees: FeeConfig,
) -> PositionSizing | None:
    raw_qff_contracts = strategy.leg_notional_twd / (
        qff_price * fees.qff_contract_multiplier
    )
    qff_contract_count = round_half_up_nonnegative(raw_qff_contracts)
    if qff_contract_count == 0:
        return None

    actual_leg_notional_twd = (
        qff_contract_count * fees.qff_contract_multiplier * qff_price
    )
    tsm_units = actual_leg_notional_twd / tsm_contract_twd_price(tsm_price, fees)
    qff_units = qff_contract_count * fees.qff_contract_multiplier

    if direction == Direction.SHORT_TSM_LONG_QFF:
        return PositionSizing(
            tsm_units=-tsm_units,
            qff_units=qff_units,
            qff_contracts=qff_contract_count,
            raw_qff_contracts=raw_qff_contracts,
            actual_leg_notional_twd=actual_leg_notional_twd,
        )
    return PositionSizing(
        tsm_units=tsm_units,
        qff_units=-qff_units,
        qff_contracts=-qff_contract_count,
        raw_qff_contracts=raw_qff_contracts,
        actual_leg_notional_twd=actual_leg_notional_twd,
    )


def tsm_contract_twd_price(tsm_twd_fair: float, fees: FeeConfig) -> float:
    multiplier = float(fees.tsm_contract_multiplier)
    if multiplier <= 0:
        raise ValueError(f"Expected a positive TSM contract multiplier, got {multiplier}")
    return tsm_twd_fair * multiplier
