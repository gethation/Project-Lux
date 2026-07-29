from __future__ import annotations

from ..config import FeeConfig
from .models import OrderSide
from .sizing import round_half_up_nonnegative
from .sizing import umc_contract_twd_price


def _umc_fee_twd(
    *,
    umc_units: float,
    umc_price: float,
    fees: FeeConfig,
    umc_side: OrderSide | None,
    usd_twd_rate: float | None,
) -> float:
    if fees.umc_fee_model != "ibkr":
        return (
            abs(umc_units)
            * umc_contract_twd_price(umc_price, fees)
            * fees.umc_fee_bps
            / 10000.0
        )

    # Imported here so replay never needs the venue package installed.
    from ..integrations.ibkr.fees import umc_trade_cost

    if umc_side is None or usd_twd_rate is None or usd_twd_rate <= 0:
        # Falling back to bps would silently charge a different, wrong number.
        raise ValueError(
            "fees.umc_fee_model='ibkr' needs umc_side and a positive "
            "usd_twd_rate; got "
            f"side={umc_side!r}, rate={usd_twd_rate!r}"
        )
    # umc_contract_twd_price is the TWD price of one ADR share; dividing by the
    # rate recovers the USD price IBKR actually bills against.
    price_usd = umc_contract_twd_price(umc_price, fees) / float(usd_twd_rate)
    cost = umc_trade_cost(
        shares=int(round(abs(umc_units))),
        price_usd=price_usd,
        side=umc_side,
    )
    return cost.total_twd(usd_twd_rate)


def fill_costs(
    *,
    umc_units: float,
    umc_price: float,
    ccf_contracts: int,
    ccf_price: float,
    fees: FeeConfig,
    umc_side: OrderSide | None = None,
    usd_twd_rate: float | None = None,
) -> dict[str, float]:
    """Costs for one fill of both legs.

    ``umc_side`` and ``usd_twd_rate`` are only read by the 'ibkr' fee model,
    which needs the side (regulatory fees are charged to the seller alone) and
    the rate (IBKR bills in USD). Under 'bps' -- the default, and what replay
    uses -- they are ignored, so the golden is unaffected either way.
    """
    umc_fee_twd = _umc_fee_twd(
        umc_units=umc_units,
        umc_price=umc_price,
        fees=fees,
        umc_side=umc_side,
        usd_twd_rate=usd_twd_rate,
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
