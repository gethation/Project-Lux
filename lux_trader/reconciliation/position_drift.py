"""Notice mid-hold that the UMC leg has moved without us.

UMC is the only leg a third party can close on its own. A stock-loan recall
buys in a short position whenever the lender asks for it back, and IBKR does
not need our consent. CCF cannot do this: TAIFEX futures have no borrow, so the
only involuntary close is an exchange liquidation, which margin monitoring
already watches for.

Until now the gap between that happening and us noticing was the rest of the
hold -- hours, or overnight. The three reconciliation points are startup,
post-trade, and post-trade again; none of them fires while a position simply
sits there.

This closes it to the margin check's own interval, and costs nothing to run.
The IBKR read-only broker has no lightweight fetch_margins, so the margin check
already pulls its FULL snapshot -- positions included. The comparison is free;
it was simply never made.

DELIBERATELY UMC-ONLY. Fubon is not polled here. margin/service.py prefers the
lightweight fetch_margins precisely because Fubon's query_single_position is
rate-limited (業務系統流量控管) under frequent polling, and adding a position
query to a 15-minute loop would walk straight into a limit someone previously
worked around on purpose. Since recall risk exists only on the US leg, the
cheap check is also the complete one.

Detection alone does not repair anything -- Phase D6 owns the response. What it
buys is that the pre-order guard in execution/position_guard.py is no longer
the FIRST place a recall is noticed, only the last.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.models import BrokerName
from .models import BrokerAccountSnapshot


@dataclass(frozen=True)
class PositionDrift:
    broker: BrokerName
    symbol: str
    expected: float
    observed: float
    drifted: bool
    detail: str

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "broker": self.broker.value,
            "symbol": self.symbol,
            "expected": self.expected,
            "observed": self.observed,
            "drifted": self.drifted,
            "detail": self.detail,
        }


def umc_position_from(snapshot: BrokerAccountSnapshot, symbol: str) -> float:
    """Signed UMC quantity in a snapshot, summing any split rows.

    Absent means zero here, which is safe in this direction: the caller only
    reaches this with a snapshot it successfully fetched, and a held position
    that the broker omits reads as drift rather than as agreement.
    """
    wanted = symbol.strip().upper()
    return sum(
        float(position.quantity)
        for position in snapshot.positions
        if str(position.symbol).strip().upper() == wanted
    )


def compare_umc_position(
    snapshot: BrokerAccountSnapshot,
    *,
    symbol: str,
    expected_units: float,
    tolerance: float = 1e-6,
) -> PositionDrift:
    observed = umc_position_from(snapshot, symbol)
    difference = observed - float(expected_units)
    drifted = abs(difference) > float(tolerance)

    if not drifted:
        detail = "matches the strategy position"
    elif expected_units and observed == 0.0:
        detail = (
            f"strategy holds {expected_units:+g} but IBKR reports none -- "
            "the leg was closed without us (recall, or a manual close)"
        )
    elif expected_units and abs(observed) < abs(expected_units):
        detail = (
            f"strategy holds {expected_units:+g}, IBKR reports {observed:+g} -- "
            "partially closed without us"
        )
    else:
        detail = f"strategy holds {expected_units:+g}, IBKR reports {observed:+g}"

    return PositionDrift(
        broker=BrokerName.IBKR_UMC,
        symbol=symbol,
        expected=float(expected_units),
        observed=observed,
        drifted=drifted,
        detail=detail,
    )


def report_position_drift(
    drift: PositionDrift,
    *,
    checked_at: Any,
    store: Any,
    reporter: Any,
) -> None:
    """Record it and shout. Routed to error, which reaches the ntfy errors topic.

    An uncovered CCF leg in a strategy that believes it is market neutral is not
    a warning-level event, and it stays unrepaired until an operator acts.
    """
    payload = drift.to_jsonable()
    store.record_event(
        -1,
        checked_at,
        "umc_position_drift",
        drift.detail,
        payload,
    )
    reporter.error(checked_at, f"umc_position_drift {drift.detail}")


__all__ = [
    "PositionDrift",
    "compare_umc_position",
    "report_position_drift",
    "umc_position_from",
]
