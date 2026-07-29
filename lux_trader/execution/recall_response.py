"""What to do about a UMC leg that was closed without us.

THIS IS THE FIRST TIME THIS SYSTEM PLACES AN ORDER IN AN ABNORMAL STATE. Every
other anomaly path pauses and waits for a person. The exception is justified by
what the alternative costs: after a recall the CCF leg is outright directional,
in a strategy whose entire premise is being market neutral, and waiting for an
operator means holding that exposure for however long they take to notice.

So the rule is narrow, and the narrowness is the safety:

  1. CLOSE ONLY. The response may reduce the CCF position toward the hedge the
     remaining UMC supports, or close it entirely. If the arithmetic ever says
     "increase" or "flip sign", that is refused outright -- an emergency unwind
     that can open a position is not an unwind.

  2. SIZED FROM THE BROKER. The quantity comes from what Fubon and IBKR report,
     never from internal state. Internal state is what we already know to be
     wrong; using it here would size the fix with the bug.

  3. REFUSAL IS AN OUTCOME. Anything unexpected produces a refusal that the
     caller must escalate and pause on. Nothing here retries quietly.

The hedge ratio itself does come from the recorded entry, and that is not a
contradiction: the ratio is a fact about a trade that happened, while the
quantities are claims about the present. Only the latter are in doubt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.models import BrokerName, OrderSide


# Distinct so callers can branch without string-matching a message.
ACTION_NOTHING = "nothing"
ACTION_REDUCE = "reduce"
ACTION_CLOSE_ALL = "close_all"
ACTION_REFUSE = "refuse"


@dataclass(frozen=True)
class RecallResponse:
    action: str
    target_ccf_contracts: int
    close_contracts: int
    close_side: OrderSide | None
    reason: str

    @property
    def requires_order(self) -> bool:
        return self.action in {ACTION_REDUCE, ACTION_CLOSE_ALL}

    @property
    def refused(self) -> bool:
        return self.action == ACTION_REFUSE

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "target_ccf_contracts": self.target_ccf_contracts,
            "close_contracts": self.close_contracts,
            "close_side": self.close_side.value if self.close_side else None,
            "reason": self.reason,
        }


def _refuse(reason: str) -> RecallResponse:
    return RecallResponse(
        action=ACTION_REFUSE,
        target_ccf_contracts=0,
        close_contracts=0,
        close_side=None,
        reason=reason,
    )


def hedge_target_contracts(
    *,
    entry_ccf_contracts: int,
    entry_umc_units: float,
    remaining_umc_units: float,
) -> int:
    """CCF contracts the remaining UMC still supports, to the nearest contract.

    Nearest rather than toward zero: rounding down leaves the pair net short the
    US leg, rounding up leaves it net long, and neither is safer in principle.
    Nearest is what minimises the residual directional exposure, which is the
    thing actually being reduced.
    """
    if entry_umc_units == 0:
        raise ValueError("entry_umc_units must not be zero")
    ratio = remaining_umc_units / entry_umc_units
    exact = entry_ccf_contracts * ratio
    # round-half-away-from-zero, so a .5 residual reduces rather than lingers
    return int(exact + (0.5 if exact >= 0 else -0.5))


def plan_recall_response(
    *,
    entry_ccf_contracts: int,
    entry_umc_units: float,
    observed_ccf_contracts: float,
    observed_umc_units: float,
) -> RecallResponse:
    """Decide the CCF close, from broker-reported quantities only."""
    if entry_umc_units == 0:
        return _refuse("entry had no UMC leg to compute a hedge ratio from")
    if observed_ccf_contracts == 0:
        return RecallResponse(
            action=ACTION_NOTHING,
            target_ccf_contracts=0,
            close_contracts=0,
            close_side=None,
            reason="Fubon reports no CCF position; nothing to unwind",
        )
    if float(observed_ccf_contracts) != int(observed_ccf_contracts):
        return _refuse(
            f"Fubon reports {observed_ccf_contracts} contracts, which is not whole"
        )

    observed_ccf = int(observed_ccf_contracts)
    target = hedge_target_contracts(
        entry_ccf_contracts=entry_ccf_contracts,
        entry_umc_units=entry_umc_units,
        remaining_umc_units=observed_umc_units,
    )

    # --- assertion 1: close only ---------------------------------------
    if abs(target) > abs(observed_ccf):
        return _refuse(
            f"target {target:+d} exceeds the held {observed_ccf:+d}: an unwind "
            "must never increase a position"
        )
    if target != 0 and (target > 0) != (observed_ccf > 0):
        return _refuse(
            f"target {target:+d} flips the sign of the held {observed_ccf:+d}: "
            "an unwind must never reverse a position"
        )

    close_contracts = abs(observed_ccf) - abs(target)
    if close_contracts == 0:
        return RecallResponse(
            action=ACTION_NOTHING,
            target_ccf_contracts=target,
            close_contracts=0,
            close_side=None,
            reason=(
                f"remaining UMC still supports the held {observed_ccf:+d} contracts"
            ),
        )

    # Closing a long sells; closing a short buys.
    close_side = OrderSide.SELL if observed_ccf > 0 else OrderSide.BUY
    return RecallResponse(
        action=ACTION_CLOSE_ALL if target == 0 else ACTION_REDUCE,
        target_ccf_contracts=target,
        close_contracts=close_contracts,
        close_side=close_side,
        reason=(
            f"UMC {observed_umc_units:+g} of {entry_umc_units:+g} remains; "
            f"CCF {observed_ccf:+d} -> {target:+d}"
        ),
    )


def build_recall_unwind_plan(
    response: RecallResponse,
    *,
    ccf_symbol: str,
    timestamp: Any,
    price: float = 0.0,
) -> "PairExecutionPlan":
    """A single close-only CCF leg, as an ordinary execution plan.

    Deliberately routed through the normal plan/adapter path rather than a
    private order call: the recall unwind then gets the same recording, the same
    fill confirmation, and the same audit trail as every other order. An
    emergency path with its own plumbing is an emergency path nobody has tested.
    """
    from .intent import ExecutionLeg, ExecutionPlanType, PairExecutionPlan

    if not response.requires_order or response.close_side is None:
        raise ValueError(f"recall response has no order to build: {response.action}")

    return PairExecutionPlan(
        plan_id=f"RECALL-UNWIND-{ccf_symbol}-{response.close_contracts}",
        plan_type=ExecutionPlanType.EXIT,
        direction=None,
        timestamp=timestamp,
        row_index=-1,
        legs=(
            ExecutionLeg(
                broker=BrokerName.FUBON_CCF,
                symbol=ccf_symbol,
                side=response.close_side,
                quantity=float(response.close_contracts),
                price=price,
                timestamp=timestamp,
                row_index=-1,
                ccf_symbol=ccf_symbol,
            ),
        ),
        reason="umc_recall_unwind",
        ccf_symbol=ccf_symbol,
    )


def recall_budget_exhausted(recalls_today: int, max_daily_recalls: int) -> bool:
    """Whether to stop for the day instead of unwinding and re-arming.

    Returning to FLAT after an unwind means the strategy may immediately re-enter
    the same short it could not borrow, and be recalled again -- a loop that
    trades fees away one round at a time. One recall can be a single lender
    changing its mind; a second in a day says the borrow itself is unreliable.
    """
    return recalls_today >= max(1, int(max_daily_recalls))


__all__ = [
    "ACTION_CLOSE_ALL",
    "ACTION_NOTHING",
    "ACTION_REDUCE",
    "ACTION_REFUSE",
    "RecallResponse",
    "build_recall_unwind_plan",
    "hedge_target_contracts",
    "plan_recall_response",
    "recall_budget_exhausted",
]
