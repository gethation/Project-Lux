from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.config import MarginManagementConfig
from lux_trader.margin.policy import MarginReading, evaluate_margin_policy


NOTIONAL = 1_000_000.0
USDTTWD = 31.8


def config(**overrides) -> MarginManagementConfig:
    return MarginManagementConfig(enabled=True, **overrides)


def ts() -> datetime:
    return datetime.fromisoformat("2026-07-06T10:00:00+08:00")


def binance(equity_usdt: float | None, maint_usdt: float | None = None) -> MarginReading:
    return MarginReading(
        venue="binance", equity=equity_usdt, maint_margin=maint_usdt, currency="USDT"
    )


def fubon(equity_twd: float | None, maint_twd: float | None = None) -> MarginReading:
    return MarginReading(
        venue="fubon", equity=equity_twd, maint_margin=maint_twd, currency="TWD"
    )


def evaluate(
    *,
    binance_reading: MarginReading,
    fubon_reading: MarginReading,
    position_open: bool = True,
    check_type: str = "daily",
    cfg: MarginManagementConfig | None = None,
    usdttwd: float | None = USDTTWD,
):
    return evaluate_margin_policy(
        binance=binance_reading,
        fubon=fubon_reading,
        config=cfg or config(),
        leg_notional_twd=NOTIONAL,
        usdttwd_rate=usdttwd,
        position_open=position_open,
        checked_at=ts(),
        check_type=check_type,
    )


def usdt(ratio: float) -> float:
    """USDT equity that produces the given TWD ratio."""
    return ratio * NOTIONAL / USDTTWD


# --- level boundaries (PoC doc §3: Binance 11%/5%, Fubon 20%/13.5%) --------


def test_both_at_target_is_ok() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.30)),
        fubon_reading=fubon(300_000.0),
    )
    assert decision.level == "ok"
    assert decision.binance.ratio == pytest.approx(0.30)
    assert decision.fubon.ratio == pytest.approx(0.30)
    assert decision.transfer_amount_twd is None
    assert "不需轉帳" in decision.guidance


def test_binance_below_transfer_threshold_requests_transfer_from_fubon() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.10)),
        fubon_reading=fubon(500_000.0),
    )
    assert decision.level == "transfer"
    assert decision.binance.level == "transfer"
    assert decision.fubon.level == "ok"
    # amount = (30% - 10%) x 1M = 200k
    assert decision.transfer_amount_twd == pytest.approx(200_000.0)
    assert decision.transfer_direction == "fubon->binance"
    assert "10:00" in decision.guidance
    assert "17:00" in decision.guidance


def test_fubon_below_transfer_threshold_requests_transfer_from_binance() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.41)),
        fubon_reading=fubon(0.19 * NOTIONAL),
    )
    assert decision.level == "transfer"
    assert decision.transfer_amount_twd == pytest.approx(0.11 * NOTIONAL)
    assert decision.transfer_direction == "binance->fubon"


def test_transfer_threshold_boundary_is_strictly_below() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.11)),
        fubon_reading=fubon(0.20 * NOTIONAL),
    )
    assert decision.level == "ok"


def test_binance_red_line_by_ratio() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.049)),
        fubon_reading=fubon(550_000.0),
    )
    assert decision.level == "red_line"
    assert "立即平倉" in decision.guidance
    assert decision.transfer_amount_twd is None


def test_fubon_red_line_by_ratio_during_red_line_check() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.55)),
        fubon_reading=fubon(0.13 * NOTIONAL),
        check_type="red_line",
    )
    assert decision.level == "red_line"
    assert decision.fubon.level == "red_line"


def test_red_line_uses_api_maintenance_margin_when_more_severe() -> None:
    # ratio 20% is fine, but equity < maint x 1.3 must still trip the red line
    decision = evaluate(
        binance_reading=binance(usdt(0.20), maint_usdt=usdt(0.20) / 1.2),
        fubon_reading=fubon(0.30 * NOTIONAL),
        check_type="red_line",
    )
    assert decision.level == "red_line"
    assert any("maintenance margin" in r for r in decision.binance.reasons)


def test_red_line_check_does_not_emit_transfer_level() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.10)),
        fubon_reading=fubon(0.30 * NOTIONAL),
        check_type="red_line",
    )
    assert decision.level == "ok"


def test_no_red_line_when_flat() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.03)),
        fubon_reading=fubon(0.30 * NOTIONAL),
        position_open=False,
    )
    # flat below transfer threshold -> still a transfer request, not red line
    assert decision.binance.level == "transfer"
    assert decision.level == "transfer"


def test_flat_below_target_is_rebalance() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.25)),
        fubon_reading=fubon(0.30 * NOTIONAL),
        position_open=False,
    )
    assert decision.level == "rebalance"
    assert decision.transfer_amount_twd == pytest.approx(0.05 * NOTIONAL)
    assert decision.transfer_direction == "fubon->binance"
    assert "不急" in decision.guidance


def test_both_deficient_routes_transfer_from_external() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.10)),
        fubon_reading=fubon(0.15 * NOTIONAL),
    )
    assert decision.level == "transfer"
    assert decision.transfer_direction.startswith("external->")


# --- data availability ------------------------------------------------------


def test_missing_equity_reports_reason_and_stays_ok() -> None:
    decision = evaluate(
        binance_reading=binance(None),
        fubon_reading=fubon(0.30 * NOTIONAL),
    )
    assert decision.binance.ratio is None
    assert "equity_unavailable" in decision.binance.reasons
    assert decision.level == "ok"
    assert "binance=NA" in decision.guidance


def test_missing_usdttwd_rate_degrades_binance_side_only() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.30)),
        fubon_reading=fubon(0.15 * NOTIONAL),
        usdttwd=None,
    )
    assert decision.binance.ratio is None
    assert "usdttwd_rate_unavailable" in decision.binance.reasons
    assert decision.fubon.level == "transfer"
    assert decision.level == "transfer"


def test_margin_config_leg_notional_override() -> None:
    decision = evaluate(
        binance_reading=binance(usdt(0.30)),
        fubon_reading=fubon(300_000.0),
        cfg=config(leg_notional_twd=2_000_000.0),
    )
    # same equity over doubled notional halves the ratios
    assert decision.binance.ratio == pytest.approx(0.15)
    assert decision.fubon.ratio == pytest.approx(0.15)


# --- PoC simulator scenarios (analysis doc §7) ------------------------------


def test_scenario_no_transfer_needed_at_30_percent_buffer() -> None:
    # §2.2: at 30% target the 3.6-month replay saw zero transfers; lowest
    # touched ratios were 24.5% / 20.3% — both must stay 'ok'.
    decision = evaluate(
        binance_reading=binance(usdt(0.245)),
        fubon_reading=fubon(0.203 * NOTIONAL),
    )
    assert decision.level == "ok"


def test_scenario_exactly_one_transfer() -> None:
    # §2.2 25% target run: lowest Binance ratio 10.3% < 11% threshold -> one
    # transfer topping back to target.
    decision = evaluate(
        binance_reading=binance(usdt(0.103)),
        fubon_reading=fubon(0.35 * NOTIONAL),
    )
    assert decision.level == "transfer"
    assert decision.transfer_amount_twd == pytest.approx((0.30 - 0.103) * NOTIONAL)


def test_scenario_maintenance_margin_breach_is_red_line() -> None:
    # §7-3: the 13% funding scenario must trip the red-line alert (Fubon red
    # line is 13.5%).
    decision = evaluate(
        binance_reading=binance(usdt(0.30)),
        fubon_reading=fubon(0.13 * NOTIONAL),
        check_type="red_line",
    )
    assert decision.level == "red_line"
