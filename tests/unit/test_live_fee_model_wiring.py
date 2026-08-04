"""The 'ibkr' fee model, reachable from the live path at last.

It was selectable in config and unreachable in practice: all five fill_costs()
call sites in runtime/live/modes.py omitted umc_side and usd_twd_rate, and the
model raises rather than quietly charging a different number. Selecting it
killed the first CCF/UMC dry run on its first entry signal (2026-08-04).

The blocker was that MarketBar carried no USD/TWD rate -- umc_twd_fair is
umc_usd * rate / 5 and cannot be inverted without one of its factors, while
IBKR bills in USD.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.config import FeeConfig
from lux_trader.core.fees import fill_costs
from lux_trader.core.models import MarketBar, OrderSide
from lux_trader.integrations.ibkr.fees import umc_trade_cost
from lux_trader.runtime.live.modes import umc_trade_side


RATE = 32.48603
UMC_USD = 18.41


def fee_config(model: str) -> FeeConfig:
    return FeeConfig(
        umc_fee_bps=2.5,
        ccf_fee_per_contract_twd=88.0,
        ccf_tax_rate=0.00002,
        ccf_contract_multiplier=2000.0,
        umc_contract_multiplier=5.0,
        umc_fee_model=model,
    )


# --- the side, which is the easy thing to get backwards -----------------------


def test_entry_side_follows_the_position_being_opened() -> None:
    assert umc_trade_side(389.0) == OrderSide.BUY
    assert umc_trade_side(-389.0) == OrderSide.SELL


def test_exit_side_is_the_negation_of_the_position() -> None:
    """THE TRAP. On an exit, fill_costs is handed the POSITION while the order
    sent is its negation, so the call site must negate before asking for a
    side. SEC and FINRA fees fall on the seller alone, so getting this
    backwards under-charges half of all trades and the totals still look
    plausible."""
    long_position = 389.0
    assert umc_trade_side(-long_position) == OrderSide.SELL

    short_position = -389.0
    assert umc_trade_side(-short_position) == OrderSide.BUY


# --- the bar now carries the rate ---------------------------------------------


def test_market_bar_defaults_the_rate_to_none() -> None:
    """Optional, because a bar can predate the field -- an older store, a CSV
    without the column. The fee model refuses in that case rather than guess."""
    bar = MarketBar(
        row_index=0,
        timestamp=datetime.now().astimezone(),
        ccf_close=116.0,
        ccf_close_filled=116.0,
        umc_twd_fair=119.6,
        spread=2.5,
    )
    assert bar.usd_twd is None


def test_the_live_minute_builder_records_the_rate() -> None:
    from lux_trader.market_data import LiveMinuteBarBuilder, LiveQuoteSet
    from lux_trader.market_data.types import LiveQuote

    def quote(source: str, ts: str, price: float) -> LiveQuote:
        return LiveQuote(
            source=source,
            symbol=source,
            timestamp=datetime.fromisoformat(ts),
            price=price,
        )

    builder = LiveMinuteBarBuilder(
        stale_seconds=60.0,
        usd_twd_stale_seconds=600.0,
        max_leg_timestamp_skew_seconds=30.0,
    )
    builder.last_ccf_close = 116.0
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-08-04T02:45:59+08:00", 116.0),
        umc=quote("umc", "2026-08-04T02:45:59+08:00", UMC_USD),
        usd_twd=quote("usd", "2026-08-04T02:45:59+08:00", RATE),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-08-04T02:46:01+08:00", 116.5),
        umc=quote("umc", "2026-08-04T02:46:01+08:00", UMC_USD),
        usd_twd=quote("usd", "2026-08-04T02:46:01+08:00", RATE),
    )

    builder.update(first, datetime.fromisoformat("2026-08-04T02:45:59+08:00"))
    result = builder.update(second, datetime.fromisoformat("2026-08-04T02:46:01+08:00"))

    assert result is not None and result.bar is not None
    assert result.bar.usd_twd == pytest.approx(RATE)
    # And it is genuinely the factor that produced umc_twd_fair.
    assert result.bar.umc_twd_fair == pytest.approx(UMC_USD * RATE / 5.0)


# --- what the model actually charges once it can be reached -------------------


def live_shaped_costs(umc_units: float, side: OrderSide) -> dict[str, float]:
    return fill_costs(
        umc_units=umc_units,
        umc_price=UMC_USD * RATE / 5.0,
        ccf_contracts=1,
        ccf_price=116.25,
        fees=fee_config("ibkr"),
        umc_side=side,
        usd_twd_rate=RATE,
    )


def test_a_live_shaped_call_now_succeeds_and_charges_per_share() -> None:
    """The regression: this exact call raised ValueError on 2026-08-04."""
    costs = live_shaped_costs(-389.0, OrderSide.SELL)

    expected = umc_trade_cost(
        shares=389, price_usd=UMC_USD, side=OrderSide.SELL
    ).total_usd * RATE
    assert costs["umc_fee_twd"] == pytest.approx(expected, rel=1e-9)


def test_the_seller_pays_more_than_the_buyer_for_the_same_size() -> None:
    """The asymmetry a bps model cannot express, now actually reachable."""
    buy = live_shaped_costs(389.0, OrderSide.BUY)
    sell = live_shaped_costs(-389.0, OrderSide.SELL)

    assert sell["umc_fee_twd"] > buy["umc_fee_twd"]


def test_a_bar_without_a_rate_still_refuses_rather_than_falling_back() -> None:
    """The refusal is the point and must survive this change: a silent fallback
    to bps would produce a live cost that looks like it came from the
    configured model and did not."""
    with pytest.raises(ValueError, match="needs umc_side and a positive"):
        fill_costs(
            umc_units=-389.0,
            umc_price=UMC_USD * RATE / 5.0,
            ccf_contracts=1,
            ccf_price=116.25,
            fees=fee_config("ibkr"),
            umc_side=OrderSide.SELL,
            usd_twd_rate=None,  # what a pre-field bar hands it
        )


def test_the_bps_model_ignores_the_new_arguments_entirely() -> None:
    """Replay passes them now too; the golden must not move because of it."""
    without = fill_costs(
        umc_units=-389.0,
        umc_price=UMC_USD * RATE / 5.0,
        ccf_contracts=1,
        ccf_price=116.25,
        fees=fee_config("bps"),
    )
    with_args = fill_costs(
        umc_units=-389.0,
        umc_price=UMC_USD * RATE / 5.0,
        ccf_contracts=1,
        ccf_price=116.25,
        fees=fee_config("bps"),
        umc_side=OrderSide.SELL,
        usd_twd_rate=RATE,
    )

    assert without == with_args
