from __future__ import annotations

from .config import FeeConfig
from .sizing import round_half_up_nonnegative


def fill_costs(
    *,
    tsm_units: float,
    tsm_price: float,
    qff_contracts: int,
    qff_price: float,
    fees: FeeConfig,
) -> dict[str, float]:
    tsm_fee_twd = abs(tsm_units) * tsm_price * fees.tsm_fee_bps / 10000.0
    qff_fee_twd = abs(qff_contracts) * fees.qff_fee_per_contract_twd
    qff_tax_per_contract_twd = round_half_up_nonnegative(
        qff_price * fees.qff_contract_multiplier * fees.qff_tax_rate
    )
    qff_tax_twd = abs(qff_contracts) * qff_tax_per_contract_twd
    return {
        "tsm_fee_twd": tsm_fee_twd,
        "qff_fee_twd": qff_fee_twd,
        "qff_tax_twd": qff_tax_twd,
        "total_fee_twd": tsm_fee_twd + qff_fee_twd + qff_tax_twd,
    }
