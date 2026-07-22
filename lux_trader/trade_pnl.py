from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from typing import Any


@dataclass(frozen=True)
class TradePnlSummary:
    available: bool
    net_pnl_twd: float | None = None
    gross_pnl_twd: float | None = None
    total_fee_twd: float | None = None


def trade_pnl_from_execution(
    plan: Any,
    outcome: Any,
    result: Any,
) -> TradePnlSummary | None:
    """Return the authoritative trade payload only for a filled exit."""

    plan_type_value = getattr(plan, "plan_type", None)
    status_value = getattr(outcome, "status", None)
    plan_type = str(getattr(plan_type_value, "value", plan_type_value or ""))
    status = str(getattr(status_value, "value", status_value or ""))
    if plan_type != "exit" or status != "filled":
        return None

    trade = getattr(result, "trade", None)
    if not isinstance(trade, Mapping):
        return TradePnlSummary(available=False)

    values = []
    for key in ("net_pnl_twd", "gross_pnl_twd", "total_fee_twd"):
        value = trade.get(key)
        if isinstance(value, bool) or not isinstance(value, Real):
            return TradePnlSummary(available=False)
        parsed = float(value)
        if not math.isfinite(parsed):
            return TradePnlSummary(available=False)
        values.append(parsed)

    return TradePnlSummary(
        available=True,
        net_pnl_twd=values[0],
        gross_pnl_twd=values[1],
        total_fee_twd=values[2],
    )


def format_trade_pnl_values(summary: TradePnlSummary) -> str | None:
    if not summary.available:
        return None
    return (
        f"net={format_twd(summary.net_pnl_twd)} "
        f"gross={format_twd(summary.gross_pnl_twd)} "
        f"fees={format_twd(summary.total_fee_twd)}"
    )


def format_twd(value: float | None) -> str:
    if value is None:
        raise ValueError("TWD value is unavailable")
    return f"{value:,.0f}"
