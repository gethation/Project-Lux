from __future__ import annotations

from ..config import FeeConfig
from .sizing import round_half_up_nonnegative
from .sizing import umc_contract_twd_price


def fill_costs(
    *,
    umc_units: float,
    umc_price: float,
    ccf_contracts: int,
    ccf_price: float,
    fees: FeeConfig,
) -> dict[str, float]:
    umc_fee_twd = (
        abs(umc_units)
        * umc_contract_twd_price(umc_price, fees)
        * fees.umc_fee_bps
        / 10000.0
    )
    ccf_fee_twd = abs(ccf_contracts) * fees.ccf_fee_per_contract_twd
    ccf_tax_per_contract_twd = round_half_up_nonnegative(
        ccf_price * fees.ccf_contract_multiplier * fees.ccf_tax_rate
    )
    ccf_tax_twd = abs(ccf_contracts) * ccf_tax_per_contract_twd
    return {
        "umc_fee_twd": umc_fee_twd,
        "ccf_fee_twd": ccf_fee_twd,
        "ccf_tax_twd": ccf_tax_twd,
        "total_fee_twd": umc_fee_twd + ccf_fee_twd + ccf_tax_twd,
    }
