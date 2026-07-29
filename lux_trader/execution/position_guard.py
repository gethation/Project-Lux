"""Verify the broker's actual position before sending a two-leg order.

The failure this exists to stop is specific and quiet. A third party can close
one of our legs without asking -- a stock-loan recall buys in a short UMC
position mid-session, and IBKR does not need our consent. Afterwards the broker
holds nothing while our state still says OPEN. Nothing notices, because nothing
looks.

The damage arrives later, at the exit. The strategy sends "buy back the UMC
short", the short is gone, and that buy OPENS A NEW LONG -- leaving long CCF and
long UMC, a doubled directional bet in a strategy whose entire premise is being
market neutral. Post-trade reconciliation catches it afterwards, which is to say
after the position exists.

So orders are sized and directed from what the broker reports, and a plan that
disagrees with the broker is refused rather than adjusted. Refusing costs one
missed exit. Adjusting silently means trading on a model of the world we have
just proven wrong.

Entry plans are checked too, for the mirror case: an unexpected position means
entering would stack on top of something the system does not know it holds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..core.models import BrokerName, OrderSide
from .intent import ExecutionPlanType, PairExecutionPlan


# Signed quantity per broker, or None when the broker could not be read.
PositionReader = Callable[[BrokerName, str], float | None]


@dataclass(frozen=True)
class PositionCheck:
    broker: BrokerName
    symbol: str
    expected: float
    observed: float | None
    passed: bool
    detail: str

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "broker": self.broker.value,
            "symbol": self.symbol,
            "expected": self.expected,
            "observed": self.observed,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PositionGuardResult:
    passed: bool
    checks: tuple[PositionCheck, ...]

    def failures(self) -> tuple[PositionCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [check.to_jsonable() for check in self.checks],
        }


def signed_close_quantity(side: OrderSide, quantity: float) -> float:
    """The position an order of this side must be closing.

    A SELL closes a long, a BUY closes a short -- so the position we expect to
    find is the opposite sign to the order.
    """
    return -abs(quantity) if side == OrderSide.BUY else abs(quantity)


def verify_plan_against_broker(
    plan: PairExecutionPlan,
    *,
    read_position: PositionReader,
    tolerances: Mapping[BrokerName, float] | None = None,
) -> PositionGuardResult:
    budgets = dict(tolerances or {})
    checks: list[PositionCheck] = []

    for leg in plan.legs:
        tolerance = float(budgets.get(leg.broker, 0.0))
        try:
            observed = read_position(leg.broker, leg.symbol)
        except Exception as exc:
            checks.append(
                PositionCheck(
                    broker=leg.broker,
                    symbol=leg.symbol,
                    expected=0.0,
                    observed=None,
                    passed=False,
                    detail=f"position unreadable: {type(exc).__name__}: {exc}",
                )
            )
            continue

        if observed is None:
            # Unreadable is not the same as flat, and must never be treated as
            # it. An unverifiable position is exactly the state this refuses.
            checks.append(
                PositionCheck(
                    broker=leg.broker,
                    symbol=leg.symbol,
                    expected=0.0,
                    observed=None,
                    passed=False,
                    detail="broker did not report a position",
                )
            )
            continue

        if plan.plan_type == ExecutionPlanType.EXIT:
            expected = signed_close_quantity(leg.side, leg.quantity)
            passed = abs(observed - expected) <= tolerance
            if observed == 0.0:
                detail = (
                    "broker is flat but the plan closes "
                    f"{expected:+g}; a closing order here would OPEN a position"
                )
            elif not passed:
                detail = f"broker holds {observed:+g}, plan closes {expected:+g}"
            else:
                detail = "matches the position being closed"
        else:
            expected = 0.0
            passed = abs(observed) <= tolerance
            detail = (
                "flat before entry"
                if passed
                else f"broker already holds {observed:+g} before an entry"
            )

        checks.append(
            PositionCheck(
                broker=leg.broker,
                symbol=leg.symbol,
                expected=expected,
                observed=observed,
                passed=passed,
                detail=detail,
            )
        )

    return PositionGuardResult(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
    )


def adapter_position_reader(
    adapters: Mapping[BrokerName, Any],
) -> PositionReader:
    """Read positions from the execution adapters themselves.

    Returns None for an adapter with no position query, which the guard treats
    as a failure rather than as flat: a venue that cannot say what it holds is
    not a venue we can safely send a closing order to.
    """

    def read(broker: BrokerName, _symbol: str) -> float | None:
        adapter = adapters.get(broker)
        fetch = getattr(adapter, "fetch_position_quantity", None)
        if not callable(fetch):
            return None
        return float(fetch())

    return read


__all__ = [
    "PositionCheck",
    "PositionGuardResult",
    "PositionReader",
    "adapter_position_reader",
    "signed_close_quantity",
    "verify_plan_against_broker",
]
