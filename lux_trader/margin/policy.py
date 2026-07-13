"""Dual-account margin policy: ratios, thresholds, and transfer guidance.

Pure logic — no I/O. Policy source: PoC docs/margin_management_analysis.md.

Each account carries the full single-leg directional risk of the pair, and the
only funding window is one transfer per day initiated at 10:00 Taipei (arrives
17:00). Levels, most severe first:

- ``red_line``:   equity can be exhausted before any transfer arrives — close or
                  reduce the position immediately; do NOT wait for a transfer.
- ``transfer``:   initiate today's 10:00 transfer, amount tops the deficient
                  account back to the target ratio, funded from the other side.
- ``rebalance``:  flat and drifted below target — top up opportunistically.
- ``ok``:         nothing to do.

Ratios are account equity over per-leg notional (leg_notional_twd); the Binance
side converts USDT equity to TWD with the current USDT/TWD rate. Red-line also
cross-checks live API maintenance margin (equity < maint * multiplier) and the
more severe verdict wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from lux_trader.config import MarginManagementConfig


LEVEL_ORDER = ("ok", "rebalance", "transfer", "red_line")


@dataclass(frozen=True)
class MarginReading:
    venue: str  # "binance" | "fubon"
    equity: float | None
    maint_margin: float | None
    currency: str
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class VenueAssessment:
    venue: str
    equity_twd: float | None
    maint_margin_twd: float | None
    ratio: float | None
    level: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarginDecision:
    checked_at: datetime
    check_type: str  # "daily" | "red_line"
    level: str
    binance: VenueAssessment
    fubon: VenueAssessment
    usdttwd_rate: float | None
    transfer_amount_twd: float | None
    transfer_direction: str | None  # e.g. "fubon->binance", "external->binance"
    guidance: str
    payload: dict[str, Any] = field(default_factory=dict)


def max_level(*levels: str) -> str:
    return max(levels, key=LEVEL_ORDER.index)


def assess_venue(
    reading: MarginReading,
    *,
    notional_twd: float,
    usdttwd_rate: float | None,
    red_line_ratio: float,
    transfer_ratio: float,
    target_ratio: float,
    red_line_maint_multiplier: float,
    position_open: bool,
    check_type: str,
) -> VenueAssessment:
    if reading.equity is None:
        return VenueAssessment(
            venue=reading.venue,
            equity_twd=None,
            maint_margin_twd=None,
            ratio=None,
            level="ok",
            reasons=("equity_unavailable",),
        )

    if reading.currency.upper() in ("TWD", "NTD"):
        rate = 1.0
    else:
        rate = usdttwd_rate if usdttwd_rate is not None else None
    if rate is None:
        return VenueAssessment(
            venue=reading.venue,
            equity_twd=None,
            maint_margin_twd=None,
            ratio=None,
            level="ok",
            reasons=("usdttwd_rate_unavailable",),
        )

    equity_twd = reading.equity * rate
    maint_margin_twd = (
        reading.maint_margin * rate if reading.maint_margin is not None else None
    )
    ratio = equity_twd / notional_twd if notional_twd > 0 else None

    reasons: list[str] = []
    level = "ok"
    if ratio is not None and position_open and ratio < red_line_ratio:
        level = "red_line"
        reasons.append(f"ratio {ratio:.1%} < red line {red_line_ratio:.1%}")
    if (
        position_open
        and maint_margin_twd is not None
        and maint_margin_twd > 0
        and equity_twd < maint_margin_twd * red_line_maint_multiplier
    ):
        level = "red_line"
        reasons.append(
            f"equity {equity_twd:,.0f} < maintenance margin "
            f"{maint_margin_twd:,.0f} x {red_line_maint_multiplier:g}"
        )
    if level != "red_line" and check_type == "daily" and ratio is not None:
        if ratio < transfer_ratio:
            level = "transfer"
            reasons.append(
                f"ratio {ratio:.1%} < transfer threshold {transfer_ratio:.1%}"
            )
        elif not position_open and ratio < target_ratio:
            level = "rebalance"
            reasons.append(f"ratio {ratio:.1%} < target {target_ratio:.1%} while flat")

    return VenueAssessment(
        venue=reading.venue,
        equity_twd=equity_twd,
        maint_margin_twd=maint_margin_twd,
        ratio=ratio,
        level=level,
        reasons=tuple(reasons),
    )


def evaluate_margin_policy(
    *,
    binance: MarginReading,
    fubon: MarginReading,
    config: MarginManagementConfig,
    leg_notional_twd: float,
    usdttwd_rate: float | None,
    position_open: bool,
    checked_at: datetime,
    check_type: str,
) -> MarginDecision:
    notional = (
        config.leg_notional_twd if config.leg_notional_twd > 0 else leg_notional_twd
    )
    binance_assessment = assess_venue(
        binance,
        notional_twd=notional,
        usdttwd_rate=usdttwd_rate,
        red_line_ratio=config.binance_red_line_ratio,
        transfer_ratio=config.binance_transfer_ratio,
        target_ratio=config.target_ratio,
        red_line_maint_multiplier=config.red_line_maint_multiplier,
        position_open=position_open,
        check_type=check_type,
    )
    fubon_assessment = assess_venue(
        fubon,
        notional_twd=notional,
        usdttwd_rate=usdttwd_rate,
        red_line_ratio=config.fubon_red_line_ratio,
        transfer_ratio=config.fubon_transfer_ratio,
        target_ratio=config.target_ratio,
        red_line_maint_multiplier=config.red_line_maint_multiplier,
        position_open=position_open,
        check_type=check_type,
    )
    level = max_level(binance_assessment.level, fubon_assessment.level)

    transfer_amount_twd: float | None = None
    transfer_direction: str | None = None
    if level in ("transfer", "rebalance"):
        deficient, other = (
            (binance_assessment, fubon_assessment)
            if LEVEL_ORDER.index(binance_assessment.level)
            >= LEVEL_ORDER.index(fubon_assessment.level)
            else (fubon_assessment, binance_assessment)
        )
        if deficient.ratio is not None:
            transfer_amount_twd = max(
                (config.target_ratio - deficient.ratio) * notional, 0.0
            )
            other_needs_funds = other.level in ("transfer", "rebalance")
            source = "external" if other_needs_funds else other.venue
            transfer_direction = f"{source}->{deficient.venue}"

    guidance = build_guidance(
        level=level,
        check_type=check_type,
        binance=binance_assessment,
        fubon=fubon_assessment,
        transfer_amount_twd=transfer_amount_twd,
        transfer_direction=transfer_direction,
        target_ratio=config.target_ratio,
    )
    return MarginDecision(
        checked_at=checked_at,
        check_type=check_type,
        level=level,
        binance=binance_assessment,
        fubon=fubon_assessment,
        usdttwd_rate=usdttwd_rate,
        transfer_amount_twd=transfer_amount_twd,
        transfer_direction=transfer_direction,
        guidance=guidance,
        payload={
            "notional_twd": notional,
            "position_open": position_open,
            "binance": assessment_jsonable(binance_assessment),
            "fubon": assessment_jsonable(fubon_assessment),
        },
    )


def ratio_text(assessment: VenueAssessment) -> str:
    if assessment.ratio is None:
        return f"{assessment.venue}=NA"
    return f"{assessment.venue}={assessment.ratio:.1%}"


def build_guidance(
    *,
    level: str,
    check_type: str,
    binance: VenueAssessment,
    fubon: VenueAssessment,
    transfer_amount_twd: float | None,
    transfer_direction: str | None,
    target_ratio: float,
) -> str:
    ratios = f"{ratio_text(binance)} {ratio_text(fubon)}"
    if level == "red_line":
        sides = ", ".join(
            f"{a.venue}({'; '.join(a.reasons)})"
            for a in (binance, fubon)
            if a.level == "red_line"
        )
        return (
            f"紅線警報 {ratios} — {sides}。立即平倉/減倉，"
            "轉帳今天 17:00 才入帳，來不及救。"
        )
    if level == "transfer":
        amount = f"{transfer_amount_twd:,.0f}" if transfer_amount_twd else "?"
        return (
            f"需要轉帳 {ratios} — 今天 10:00 前發起 {transfer_direction} "
            f"約 {amount} TWD（補回 {target_ratio:.0%} 水位），17:00 入帳後"
            "請確認比率回到目標。錯過今天要再等 31 小時。"
        )
    if level == "rebalance":
        amount = f"{transfer_amount_twd:,.0f}" if transfer_amount_twd else "?"
        return (
            f"空倉再平衡 {ratios} — 不急，擇機轉 {transfer_direction} "
            f"約 {amount} TWD 回補到 {target_ratio:.0%}/{target_ratio:.0%}。"
        )
    label = "每日檢查" if check_type == "daily" else "紅線檢查"
    return f"{label}正常 {ratios} — 不需轉帳。"


def assessment_jsonable(assessment: VenueAssessment) -> dict[str, Any]:
    return {
        "venue": assessment.venue,
        "equity_twd": assessment.equity_twd,
        "maint_margin_twd": assessment.maint_margin_twd,
        "ratio": assessment.ratio,
        "level": assessment.level,
        "reasons": list(assessment.reasons),
    }
