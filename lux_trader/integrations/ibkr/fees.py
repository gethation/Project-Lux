"""What IBKR actually charges for a UMC trade.

The backtest prices this leg at a flat 2.5 bps because that is all a bps model
can express. IBKR charges per SHARE, with a per-order minimum and a cap, plus
regulatory fees that apply to sells only, plus a borrow fee on any short that is
charged per DAY HELD -- a dimension core/fees.py has no concept of at all.

WHICH WAY THE BACKTEST ERRS DEPENDS ON PRICE, AND THE PLAN GOT THIS WRONG.
docs/CCF_UMC_PLAN.md records "2.5 bps is conservative rather than optimistic:
$2.49 modelled per side vs $2.03 actual". That was measured at a single price,
$24.53, and it does not generalise. Commission is per SHARE and does not move
with price, while the bps charge scales with it, so the two cross at about
$23.2 a share: above it the model over-charges, below it the model charges LESS
than IBKR does.

The fixture's UMC ranges $18.59 to $28.88, straddling that line, so the backtest
under-charges the US leg on its cheaper days. The magnitude is small -- at worst
about $0.74 per side, roughly 0.4% of net across eighteen trades -- so the
strategy's economics are unchanged. But "conservative" was stated as a property
and it is not one, and a faster configuration paying these fixed costs ten times
more often would feel it.

Replay keeps the bps model regardless, because its job is to reproduce the PoC.

The minimum does not bind at this size. $1.00 needs fewer than 200 shares, which
is under half a CCF contract -- a size this strategy cannot trade.

RATES ARE NOT CONSTANTS OF NATURE. The SEC fee rate is reset annually and the
FINRA TAF changes by rule filing; both are named below with the date they were
recorded, and both must be confirmed against IBKR's current schedule before this
model is used to justify a live decision. Getting them stale understates cost by
cents per trade -- immaterial next to getting the SIDE wrong, which is why the
sell-only rule is what the tests pin hardest.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.models import OrderSide


# IBKR US equities, Fixed tier. Recorded 2026-07-25; confirm before live use.
COMMISSION_PER_SHARE_USD = 0.005
COMMISSION_MINIMUM_USD = 1.00
# The cap exists to stop the per-share charge exceeding the trade itself on
# very low-priced stock. UMC is a few dollars, so it can bind: at $1/share the
# per-share rate alone is 0.5% and two cents of price movement covers it.
COMMISSION_MAX_FRACTION_OF_VALUE = 0.01

# Regulatory fees. SELL SIDE ONLY -- both are levied on the seller.
# SEC Section 31 fee, per dollar of proceeds. Reset annually by the SEC.
SEC_FEE_PER_USD = 0.0000278
# FINRA Trading Activity Fee, per share sold, capped per trade.
FINRA_TAF_PER_SHARE_USD = 0.000166
FINRA_TAF_MAX_USD = 8.30

RATES_AS_OF = "2026-07-25"


@dataclass(frozen=True)
class UmcTradeCost:
    """One side of a UMC trade, in USD."""

    shares: int
    price_usd: float
    side: OrderSide
    commission_usd: float
    sec_fee_usd: float
    finra_taf_usd: float

    @property
    def total_usd(self) -> float:
        return self.commission_usd + self.sec_fee_usd + self.finra_taf_usd

    @property
    def trade_value_usd(self) -> float:
        return self.shares * self.price_usd

    def total_twd(self, usd_twd_rate: float) -> float:
        return self.total_usd * float(usd_twd_rate)

    def as_bps_of_value(self) -> float:
        """What this side would have cost expressed as bps, for comparison."""
        if self.trade_value_usd <= 0:
            return 0.0
        return self.total_usd / self.trade_value_usd * 10_000.0


def umc_commission_usd(shares: int, price_usd: float) -> float:
    per_share = abs(shares) * COMMISSION_PER_SHARE_USD
    charged = max(per_share, COMMISSION_MINIMUM_USD)
    value = abs(shares) * float(price_usd)
    if value > 0:
        charged = min(charged, value * COMMISSION_MAX_FRACTION_OF_VALUE)
    return charged


def umc_trade_cost(
    *,
    shares: int,
    price_usd: float,
    side: OrderSide,
) -> UmcTradeCost:
    """Commission plus regulatory fees for one side.

    Regulatory fees are charged to the SELLER only, so a buy and a sell of the
    same size are not the same cost. A model that averaged them would understate
    every exit of a long and overstate every entry.
    """
    shares = abs(int(shares))
    commission = umc_commission_usd(shares, price_usd)
    if side == OrderSide.SELL:
        proceeds = shares * float(price_usd)
        sec_fee = proceeds * SEC_FEE_PER_USD
        taf = min(shares * FINRA_TAF_PER_SHARE_USD, FINRA_TAF_MAX_USD)
    else:
        sec_fee = 0.0
        taf = 0.0
    return UmcTradeCost(
        shares=shares,
        price_usd=float(price_usd),
        side=side,
        commission_usd=commission,
        sec_fee_usd=sec_fee,
        finra_taf_usd=taf,
    )


def umc_borrow_cost_usd(
    *,
    shares: int,
    price_usd: float,
    annual_rate: float,
    days_held: float,
) -> float:
    """Stock-loan fee on a short, accrued per day held.

    The dimension core/fees.py does not have. It also makes the strategy
    DIRECTIONALLY ASYMMETRIC, which no backtest here models: long UMC / short
    CCF borrows nothing, while short UMC / long CCF pays for every day it is
    open. Roughly half the backtest's trades are the second kind.

    A 360-day year is the market convention for stock-loan accrual.
    """
    if annual_rate < 0:
        raise ValueError("annual_rate must not be negative")
    if days_held < 0:
        raise ValueError("days_held must not be negative")
    return abs(int(shares)) * float(price_usd) * float(annual_rate) * (
        float(days_held) / 360.0
    )


def round_trip_cost_usd(
    *,
    shares: int,
    entry_price_usd: float,
    exit_price_usd: float,
    is_short: bool,
    borrow_annual_rate: float = 0.0,
    days_held: float = 0.0,
) -> dict[str, float]:
    """Both sides plus borrow, for comparing against the bps model."""
    entry_side = OrderSide.SELL if is_short else OrderSide.BUY
    exit_side = OrderSide.BUY if is_short else OrderSide.SELL
    entry = umc_trade_cost(shares=shares, price_usd=entry_price_usd, side=entry_side)
    exit_ = umc_trade_cost(shares=shares, price_usd=exit_price_usd, side=exit_side)
    borrow = (
        umc_borrow_cost_usd(
            shares=shares,
            price_usd=entry_price_usd,
            annual_rate=borrow_annual_rate,
            days_held=days_held,
        )
        if is_short
        else 0.0
    )
    return {
        "entry_usd": entry.total_usd,
        "exit_usd": exit_.total_usd,
        "borrow_usd": borrow,
        "total_usd": entry.total_usd + exit_.total_usd + borrow,
    }


__all__ = [
    "COMMISSION_MAX_FRACTION_OF_VALUE",
    "COMMISSION_MINIMUM_USD",
    "COMMISSION_PER_SHARE_USD",
    "FINRA_TAF_MAX_USD",
    "FINRA_TAF_PER_SHARE_USD",
    "RATES_AS_OF",
    "SEC_FEE_PER_USD",
    "UmcTradeCost",
    "round_trip_cost_usd",
    "umc_borrow_cost_usd",
    "umc_commission_usd",
    "umc_trade_cost",
]
