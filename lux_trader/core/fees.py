from __future__ import annotations

from ..config import FeeConfig
from .sizing import round_half_up_nonnegative
from .sizing import us_leg_contract_twd_price


def fill_costs(
    *,
    us_leg_units: float,
    us_leg_price: float,
    tw_leg_contracts: int,
    tw_leg_price: float,
    fees: FeeConfig,
) -> dict[str, float]:
    us_leg_fee_twd = (
        abs(us_leg_units)
        * us_leg_contract_twd_price(us_leg_price, fees)
        * fees.us_leg_fee_bps
        / 10000.0
    )
    tw_leg_fee_twd = abs(tw_leg_contracts) * fees.tw_leg_fee_per_contract_twd
    tw_leg_tax_per_contract_twd = round_half_up_nonnegative(
        tw_leg_price * fees.tw_leg_contract_multiplier * fees.tw_leg_tax_rate
    )
    tw_leg_tax_twd = abs(tw_leg_contracts) * tw_leg_tax_per_contract_twd
    return {
        "us_leg_fee_twd": us_leg_fee_twd,
        "tw_leg_fee_twd": tw_leg_fee_twd,
        "tw_leg_tax_twd": tw_leg_tax_twd,
        "total_fee_twd": us_leg_fee_twd + tw_leg_fee_twd + tw_leg_tax_twd,
    }
