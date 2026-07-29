"""IBKR's real cost model for UMC, against the numbers the plan measured.

The bps model the backtest uses cannot express any of this: a per-share charge,
a per-order minimum, fees that apply to one side only, or a cost that accrues
per day held.
"""

from __future__ import annotations

import pytest

from lux_trader.core.models import OrderSide
from lux_trader.integrations.ibkr.fees import (
    COMMISSION_MINIMUM_USD,
    FINRA_TAF_MAX_USD,
    round_trip_cost_usd,
    umc_borrow_cost_usd,
    umc_commission_usd,
    umc_trade_cost,
)


# One CCF lot hedges a median 406 UMC shares (measured 2026-07-25).
MEDIAN_SHARES = 406
UMC_PRICE = 18.9


def test_the_measured_case_one_ccf_lot_costs_about_two_dollars() -> None:
    """406 x $0.005 = $2.03, the figure the plan's economics rest on."""
    assert umc_commission_usd(MEDIAN_SHARES, UMC_PRICE) == pytest.approx(2.03)


def test_the_minimum_does_not_bind_at_a_tradable_size() -> None:
    """$1.00 needs under 200 shares -- less than half a CCF contract, which is
    not a size this strategy can trade. The minimum was the open question in the
    plan and this is the answer."""
    assert umc_commission_usd(MEDIAN_SHARES, UMC_PRICE) > COMMISSION_MINIMUM_USD * 2

    # It does bind far below a tradable size.
    assert umc_commission_usd(100, UMC_PRICE) == pytest.approx(COMMISSION_MINIMUM_USD)


def test_the_cap_overrides_the_minimum_on_a_tiny_trade() -> None:
    """Order of precedence, which is easy to get backwards: the 1% cap wins.
    One share at $18.90 costs 18.9 cents, not the $1.00 minimum -- IBKR will not
    charge a dollar to trade nineteen."""
    assert umc_commission_usd(1, UMC_PRICE) == pytest.approx(UMC_PRICE * 0.01)
    assert umc_commission_usd(1, UMC_PRICE) < COMMISSION_MINIMUM_USD


def test_the_one_percent_cap_binds_on_very_cheap_stock() -> None:
    """At $0.20 a share the per-share rate alone is 2.5% of the trade."""
    capped = umc_commission_usd(1000, 0.20)

    assert capped == pytest.approx(1000 * 0.20 * 0.01)
    assert capped < 1000 * 0.005


# --- side asymmetry ----------------------------------------------------------


def test_regulatory_fees_are_charged_to_the_seller_only() -> None:
    """The thing a bps model gets structurally wrong: a buy and a sell of the
    same size are not the same cost."""
    buy = umc_trade_cost(shares=MEDIAN_SHARES, price_usd=UMC_PRICE, side=OrderSide.BUY)
    sell = umc_trade_cost(shares=MEDIAN_SHARES, price_usd=UMC_PRICE, side=OrderSide.SELL)

    assert buy.sec_fee_usd == 0.0
    assert buy.finra_taf_usd == 0.0
    assert sell.sec_fee_usd > 0.0
    assert sell.finra_taf_usd > 0.0
    assert sell.total_usd > buy.total_usd
    # Same commission on both sides, though.
    assert buy.commission_usd == pytest.approx(sell.commission_usd)


def test_the_finra_taf_is_capped_per_trade() -> None:
    huge = umc_trade_cost(shares=10_000_000, price_usd=UMC_PRICE, side=OrderSide.SELL)

    assert huge.finra_taf_usd == pytest.approx(FINRA_TAF_MAX_USD)


def bps_model_charge(shares: int, price_usd: float, bps: float = 2.5) -> float:
    return shares * price_usd * bps / 10_000.0


def test_the_bps_model_is_conservative_only_above_a_crossover_price() -> None:
    """CORRECTS A CLAIM IN THE PLAN.

    docs/CCF_UMC_PLAN.md records "2.5 bps is conservative rather than
    optimistic: $2.49 modelled per side vs $2.03 actual". That was measured at
    one price, $24.53, and it does not generalise. Commission is charged PER
    SHARE and so does not move with price, while the bps charge scales with it
    -- so below roughly $23.2 a share the model charges LESS than IBKR does.

    The fixture's UMC ranges $18.59 to $28.88, straddling that line, so the
    backtest under-charges the US leg on its cheaper days. The magnitude is
    small -- worst case about $0.74 per side, or roughly 0.4% of net across
    eighteen trades -- but "conservative" was stated as a property and it is
    not one.
    """
    below = 18.90
    above = 24.53

    assert bps_model_charge(MEDIAN_SHARES, below) < umc_trade_cost(
        shares=MEDIAN_SHARES, price_usd=below, side=OrderSide.SELL
    ).total_usd
    assert bps_model_charge(MEDIAN_SHARES, above) > umc_trade_cost(
        shares=MEDIAN_SHARES, price_usd=above, side=OrderSide.SELL
    ).total_usd


def test_the_crossover_sits_near_twenty_three_dollars() -> None:
    """Pinned so the correction above does not quietly drift."""
    low, high = 15.0, 35.0
    for _ in range(60):
        mid = (low + high) / 2
        real = umc_trade_cost(
            shares=MEDIAN_SHARES, price_usd=mid, side=OrderSide.SELL
        ).total_usd
        if bps_model_charge(MEDIAN_SHARES, mid) < real:
            low = mid
        else:
            high = mid

    assert 23.0 < (low + high) / 2 < 23.5


def test_the_sell_side_regulatory_cost_is_cents_not_dollars() -> None:
    """The omitted piece is small; it is the per-share commission that decides
    which way the bps model errs."""
    sell = umc_trade_cost(shares=MEDIAN_SHARES, price_usd=UMC_PRICE, side=OrderSide.SELL)

    assert (sell.sec_fee_usd + sell.finra_taf_usd) < 0.35


# --- borrow, the dimension core/fees.py does not have ------------------------


def test_borrow_accrues_per_day_held() -> None:
    one_day = umc_borrow_cost_usd(
        shares=MEDIAN_SHARES, price_usd=UMC_PRICE, annual_rate=0.03, days_held=1
    )
    ten_days = umc_borrow_cost_usd(
        shares=MEDIAN_SHARES, price_usd=UMC_PRICE, annual_rate=0.03, days_held=10
    )

    assert ten_days == pytest.approx(one_day * 10)
    # 360-day convention.
    assert one_day == pytest.approx(MEDIAN_SHARES * UMC_PRICE * 0.03 / 360)


def test_a_zero_rate_costs_nothing() -> None:
    assert umc_borrow_cost_usd(
        shares=MEDIAN_SHARES, price_usd=UMC_PRICE, annual_rate=0.0, days_held=30
    ) == 0.0


def test_negative_inputs_are_refused_rather_than_credited() -> None:
    with pytest.raises(ValueError):
        umc_borrow_cost_usd(
            shares=1, price_usd=UMC_PRICE, annual_rate=-0.01, days_held=1
        )
    with pytest.raises(ValueError):
        umc_borrow_cost_usd(
            shares=1, price_usd=UMC_PRICE, annual_rate=0.01, days_held=-1
        )


# --- the asymmetry the backtest does not model -------------------------------


def test_shorting_costs_more_than_going_long_for_the_same_trade() -> None:
    """Long UMC borrows nothing; short UMC pays for every day it is open. About
    half the backtest's trades are the second kind, and none of them were
    charged for it."""
    common = {
        "shares": MEDIAN_SHARES,
        "entry_price_usd": UMC_PRICE,
        "exit_price_usd": UMC_PRICE,
        "borrow_annual_rate": 0.03,
        "days_held": 2.0,
    }
    long_side = round_trip_cost_usd(**common, is_short=False)
    short_side = round_trip_cost_usd(**common, is_short=True)

    assert long_side["borrow_usd"] == 0.0
    assert short_side["borrow_usd"] > 0.0
    assert short_side["total_usd"] > long_side["total_usd"]


def test_a_two_day_short_round_trip_stays_a_small_share_of_the_trade() -> None:
    """The plan's conclusion, as a test: ~1,000 TWD of costs against 182k net
    over eight shorts is 0.55%, so the slow configuration absorbs it. This pins
    the per-trade magnitude that conclusion rests on."""
    costs = round_trip_cost_usd(
        shares=MEDIAN_SHARES,
        entry_price_usd=UMC_PRICE,
        exit_price_usd=UMC_PRICE,
        is_short=True,
        borrow_annual_rate=0.03,
        days_held=2.0,
    )
    trade_value = MEDIAN_SHARES * UMC_PRICE

    assert costs["total_usd"] / trade_value < 0.001  # under 10 bps round trip


def test_a_fast_configuration_would_multiply_the_fixed_costs() -> None:
    """Why the plan warns that a faster grid changes the answer: the per-trade
    floor is fixed, so 110 trades pay it 110 times."""
    one = round_trip_cost_usd(
        shares=MEDIAN_SHARES,
        entry_price_usd=UMC_PRICE,
        exit_price_usd=UMC_PRICE,
        is_short=True,
    )

    assert one["total_usd"] * 110 > one["total_usd"] * 16 * 2


# --- wiring into fill_costs ---------------------------------------------------


def fee_config(model: str):
    from lux_trader.config import FeeConfig

    return FeeConfig(
        umc_fee_bps=2.5,
        ccf_fee_per_contract_twd=88.0,
        ccf_tax_rate=0.00002,
        ccf_contract_multiplier=2000.0,
        umc_contract_multiplier=5.0,
        umc_fee_model=model,
    )


def test_the_default_model_is_bps_so_the_golden_is_untouched() -> None:
    """Replay's job is to reproduce the PoC, which charges bps. Anything else
    here would move a number the whole branch uses as its reference."""
    from lux_trader.core.fees import fill_costs

    costs = fill_costs(
        umc_units=-406.0,
        umc_price=UMC_PRICE * 31.8 / 5.0,  # TWD fair for a $18.90 ADR at 31.8
        ccf_contracts=1,
        ccf_price=156.0,
        fees=fee_config("bps"),
    )

    notional_twd = 406.0 * UMC_PRICE * 31.8
    assert costs["umc_fee_twd"] == pytest.approx(notional_twd * 2.5 / 10_000)


def test_the_ibkr_model_charges_what_ibkr_charges() -> None:
    from lux_trader.core.fees import fill_costs

    rate = 31.8
    costs = fill_costs(
        umc_units=-406.0,
        umc_price=UMC_PRICE * rate / 5.0,
        ccf_contracts=1,
        ccf_price=156.0,
        fees=fee_config("ibkr"),
        umc_side=OrderSide.SELL,
        usd_twd_rate=rate,
    )

    expected = umc_trade_cost(
        shares=406, price_usd=UMC_PRICE, side=OrderSide.SELL
    ).total_usd * rate
    assert costs["umc_fee_twd"] == pytest.approx(expected, rel=1e-9)


def test_the_ibkr_model_refuses_rather_than_falling_back_to_bps() -> None:
    """Silently charging a different number would be the worst of both: a live
    cost that looks like it came from the configured model and did not."""
    from lux_trader.core.fees import fill_costs

    for kwargs in (
        {},
        {"umc_side": OrderSide.SELL},
        {"usd_twd_rate": 31.8},
        {"umc_side": OrderSide.SELL, "usd_twd_rate": 0.0},
    ):
        with pytest.raises(ValueError, match="needs umc_side and a positive"):
            fill_costs(
                umc_units=-406.0,
                umc_price=120.0,
                ccf_contracts=1,
                ccf_price=156.0,
                fees=fee_config("ibkr"),
                **kwargs,
            )


def test_an_unknown_fee_model_is_rejected_at_config_load(tmp_path) -> None:
    from lux_trader.config import load_config

    path = tmp_path / "bad_model.toml"
    path.write_text(
        "\n".join(
            [
                "[paths]",
                "input_csv = ''",
                f"store_path = '{(tmp_path / 's.sqlite3').as_posix()}'",
                "",
                "[fees]",
                "ccf_contract_multiplier = 2000.0",
                "umc_fee_model = 'per_share'",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="umc_fee_model must be one of"):
        load_config(path)
