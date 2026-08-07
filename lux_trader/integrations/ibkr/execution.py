"""The UMC execution adapter: the first thing in this repo that can place a US order.

Exercised against a real Gateway on 2026-08-04: one long round trip and one
short, 1 share each, both confirmed against the account independently. The short
is the one that mattered -- `position=-1` means Reg T permission, an actual
borrow delivery, and a buy-to-cover all hold in practice rather than in an
account field, on the path roughly half the backtest's trades need.

What that run did NOT establish, because a green result invites the opposite
reading:

  * both legs at once -- this adapter places ONE leg, so it cannot produce the
    state the pair actually fears: CCF filled, UMC unknown
  * `unknown` -> PAUSE, which has only ever run against fakes
  * recall, and the proportional CCF reduction that answers it (Phase D6),
    which cannot be manufactured on demand

It also taught the confirmation path its most important lesson: see
`place_and_confirm_umc_order` in client_process.py for why a terminal order
status is not evidence, and why `failed` has to be earned.

Three things it does refuse to guess about:

  Whole shares. Sizing computes an ideal hedge ratio in fractional shares
  because the PoC never places an order. A cash equity trades in whole shares,
  so the rounding happens HERE, at the boundary where an intent becomes an
  order, rather than in sizing where it would move the replay golden. The
  residual is reported, not hidden -- measured at +-0.01% of the leg.

  Fill or no fill. The worker classifies filled / failed / unknown, and this
  maps "unknown" to an outcome that pauses. "Rejected" and "no idea" are both
  non-fills; only one of them is safe.

  Whether it may trade at all. Both env gates must be open, checked on every
  execute rather than once at construction, so a long-running process cannot
  keep a permission that was revoked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ...core.models import (
    BrokerName,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from ...core.time import TAIPEI_TZ
from ...execution.intent import ExecutionLeg, PairExecutionPlan
from ...execution.outcome import (
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    order_request_from_execution_leg,
)
from .client_process import IbkrClientProcess


# Both must be set. PROJECT_LUX_ALLOW_LIVE_ORDER is the system-wide switch;
# IBKR_ALLOW_LIVE_ORDER names the venue, so enabling Fubon never enables this.
IBKR_LIVE_ORDER_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "IBKR_ALLOW_LIVE_ORDER",
)

DEFAULT_ORDER_WAIT_SECONDS = 30.0
DEFAULT_EXECUTION_CLIENT_ID = 17_004


@dataclass(frozen=True)
class IbkrExecutionPreflight:
    position_quantity: float
    open_orders: tuple[dict[str, Any], ...]


def ibkr_live_order_gates_open() -> dict[str, bool]:
    return {
        name: os.getenv(name, "").strip() == "1" for name in IBKR_LIVE_ORDER_ENV_GATES
    }


def whole_share_quantity(quantity: float) -> tuple[int, float]:
    """Round a fractional hedge to tradable shares, returning the residual.

    Rounds toward zero rather than to nearest: overshooting the hedge means
    holding more US exposure than the CCF leg covers, and an under-hedge is the
    smaller error of the two. The residual is returned so the caller can record
    what was dropped instead of it vanishing.
    """
    shares = int(abs(quantity))
    return shares, abs(quantity) - shares


class IbkrUmcExecutionAdapter:
    """Places one UMC leg per plan, and says exactly what it knows afterwards."""

    broker = BrokerName.IBKR_UMC

    def __init__(
        self,
        symbol: str = "UMC",
        *,
        client: IbkrClientProcess | None = None,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = DEFAULT_EXECUTION_CLIENT_ID,
        order_wait_seconds: float = DEFAULT_ORDER_WAIT_SECONDS,
        clock: Any | None = None,
        require_env_gates: bool = True,
    ) -> None:
        self.symbol = str(symbol).strip().upper()
        self.order_wait_seconds = float(order_wait_seconds)
        self.require_env_gates = bool(require_env_gates)
        self.clock = clock or (lambda: datetime.now(TAIPEI_TZ))
        self._owns_client = client is None
        # readonly=False is the only place in the repo that asks for a
        # trade-capable IBKR session.
        self.client = client or IbkrClientProcess(
            host=host, port=port, client_id=client_id, readonly=False
        )

    # -- ExecutionAdapter --------------------------------------------------

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        leg = self._select_leg(plan)
        if leg is None:
            return self._rejected(plan, "plan has no IBKR UMC leg")

        closed = self._closed_env_gates()
        if closed:
            return self._rejected(
                plan, "IBKR live-order gates closed: " + ", ".join(closed)
            )

        shares, residual = whole_share_quantity(leg.quantity)
        if shares <= 0:
            return self._rejected(
                plan, f"UMC quantity {leg.quantity:g} rounds to zero shares"
            )

        action = "BUY" if leg.side == OrderSide.BUY else "SELL"
        try:
            result = self.client.place_and_confirm_umc_order(
                action=action,
                quantity=shares,
                wait_seconds=self.order_wait_seconds,
            )
        except Exception as exc:
            # The request itself failed, so whether an order reached IBKR is
            # unknowable from here. Never report this as a clean failure.
            return self._unknown(plan, leg, f"{type(exc).__name__}: {exc}")

        return self._outcome_from_result(plan, leg, result, residual=residual)

    # -- read-only helpers the coordinator's guard and the CLI use ----------

    def fetch_position_quantity(self) -> float:
        return float(self.client.fetch_umc_position())

    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.client.fetch_umc_open_orders())

    def preflight(self) -> IbkrExecutionPreflight:
        return IbkrExecutionPreflight(
            position_quantity=self.fetch_position_quantity(),
            open_orders=self.fetch_open_orders(),
        )

    def session_health(self) -> dict[str, Any]:
        return dict(self.client.session_health())

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    # -- internals ---------------------------------------------------------

    def _closed_env_gates(self) -> list[str]:
        if not self.require_env_gates:
            return []
        gates = ibkr_live_order_gates_open()
        return [name for name, is_open in gates.items() if not is_open]

    def _select_leg(self, plan: PairExecutionPlan) -> ExecutionLeg | None:
        legs = [leg for leg in plan.legs if leg.broker == BrokerName.IBKR_UMC]
        if len(legs) != 1:
            return None
        leg = legs[0]
        return leg if leg.symbol.strip().upper() == self.symbol else None

    def _outcome_from_result(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        result: dict[str, Any],
        *,
        residual: float,
    ) -> ExecutionOutcome:
        classification = str(result.get("classification", "unknown"))
        # IBKR order ids restart at 1 on every client connection, so a bare
        # number is unique only inside one session. The store keys orders
        # globally. On 2026-08-07 an exit was handed id 2 -- the id an entry had
        # used two restarts earlier -- and store.record_order raised
        # order_id_collision AFTER both legs had filled, killing the loop with
        # the pair closed at both brokers but never written down. Namespace it
        # by plan, the way the Fubon leg already does. The raw id stays in
        # payload, and client_process still uses trade.order.orderId to talk to
        # IBKR -- only the persisted key changes.
        raw_order_id = str(result.get("order_id", ""))
        id_suffix = "-".join(part for part in (plan.plan_id, raw_order_id) if part)
        order_id = f"IBKR-{id_suffix}"
        filled_shares = float(result.get("filled", 0.0))
        payload = {
            "adapter": "ibkr_umc_execution",
            "requested_quantity": leg.quantity,
            "whole_share_quantity": filled_shares,
            "rounding_residual_shares": residual,
            **result,
        }

        order = OrderResult(
            order_id=order_id,
            request=order_request_from_execution_leg(leg),
            status=(
                OrderStatus.FILLED
                if classification == "filled"
                else OrderStatus.OPEN
            ),
        )

        if classification == "filled":
            fill = Fill(
                fill_id=f"IBKR-FILL-{id_suffix}",
                order_id=order_id,
                broker=self.broker,
                symbol=leg.symbol,
                side=leg.side,
                quantity=filled_shares,
                price=float(result.get("avg_fill_price") or leg.price),
                fee_twd=leg.fee_twd,
                timestamp=leg.timestamp,
                row_index=leg.row_index,
            )
            return ExecutionOutcome(
                plan_id=plan.plan_id,
                timestamp=self.clock(),
                status=ExecutionOutcomeStatus.FILLED,
                message=f"IBKR UMC {order.request.side.value} {filled_shares:g} filled",
                orders=(order,),
                fills=(fill,),
                payload=payload,
            )

        if classification == "failed":
            # Terminal, nothing filled, position unmoved: no exposure was
            # created, so the coordinator can unwind cleanly rather than pause.
            return ExecutionOutcome(
                plan_id=plan.plan_id,
                timestamp=self.clock(),
                status=ExecutionOutcomeStatus.FAILED,
                message=f"IBKR UMC order rejected: {result.get('status')}",
                orders=(order,),
                payload=payload,
            )

        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.UNKNOWN,
            message=(
                "IBKR UMC order outcome unknown "
                f"(status={result.get('status')}, filled={filled_shares:g}) -- "
                "the position may or may not have moved"
            ),
            recommended_state=StrategyState.PAUSED,
            orders=(order,),
            payload=payload,
        )

    def _rejected(self, plan: PairExecutionPlan, message: str) -> ExecutionOutcome:
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.REJECTED,
            message=message,
            recommended_state=StrategyState.PAUSED,
            payload={"adapter": "ibkr_umc_execution", "reason": message},
        )

    def _unknown(
        self, plan: PairExecutionPlan, leg: ExecutionLeg, message: str
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.UNKNOWN,
            message=f"IBKR UMC order may have been sent: {message}",
            recommended_state=StrategyState.PAUSED,
            payload={
                "adapter": "ibkr_umc_execution",
                "stage": "place_and_confirm",
                "error": message,
                "requested_quantity": leg.quantity,
            },
        )

    def __enter__(self) -> "IbkrUmcExecutionAdapter":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_EXECUTION_CLIENT_ID",
    "DEFAULT_ORDER_WAIT_SECONDS",
    "IBKR_LIVE_ORDER_ENV_GATES",
    "IbkrExecutionPreflight",
    "IbkrUmcExecutionAdapter",
    "ibkr_live_order_gates_open",
    "whole_share_quantity",
]
