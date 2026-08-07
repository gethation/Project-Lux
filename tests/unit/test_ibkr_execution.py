"""The UMC execution adapter, against a fake worker.

Nothing here has met a real Gateway. The point of these tests is the decisions
the adapter makes about what it KNOWS -- filled, definitely-not-filled, or
unknown -- because "rejected" and "no idea" are both non-fills and only one of
them is safe.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import (
    BrokerName,
    Direction,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.execution.outcome import ExecutionOutcomeStatus
from lux_trader.integrations.ibkr.execution import (
    IBKR_LIVE_ORDER_ENV_GATES,
    IbkrUmcExecutionAdapter,
    whole_share_quantity,
)


TS = datetime.fromisoformat("2026-07-29T23:40:00+08:00")


@pytest.fixture
def gates_open(monkeypatch):
    for name in IBKR_LIVE_ORDER_ENV_GATES:
        monkeypatch.setenv(name, "1")


class FakeClient:
    def __init__(self, result=None, *, position=0.0, open_orders=()) -> None:
        self.result = result
        self.position = position
        self.open_orders = list(open_orders)
        self.calls: list[dict] = []
        self.closed = False

    def place_and_confirm_umc_order(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)

    def fetch_umc_position(self) -> float:
        return self.position

    def fetch_umc_open_orders(self):
        return list(self.open_orders)

    def close(self) -> None:
        self.closed = True


def umc_leg(side: OrderSide = OrderSide.SELL, quantity: float = 406.0) -> ExecutionLeg:
    return ExecutionLeg(
        broker=BrokerName.IBKR_UMC,
        symbol="UMC",
        side=side,
        quantity=quantity,
        price=18.9,
        timestamp=TS,
        row_index=42,
    )


def plan(*legs: ExecutionLeg) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id="PLAN-D1",
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_UMC_LONG_CCF,
        timestamp=TS,
        row_index=42,
        legs=legs or (umc_leg(),),
        reason="test",
    )


def adapter(client: FakeClient) -> IbkrUmcExecutionAdapter:
    return IbkrUmcExecutionAdapter(client=client, clock=lambda: TS)


def filled_result(**overrides):
    base = {
        "classification": "filled",
        "order_id": 7,
        "status": "Filled",
        "filled": 406.0,
        "remaining": 0.0,
        "avg_fill_price": 18.91,
    }
    base.update(overrides)
    return base


# --- env gates ---------------------------------------------------------------


def test_persisted_order_id_is_namespaced_by_plan_not_the_bare_ibkr_number(
    monkeypatch,
) -> None:
    # IBKR restarts orderId at 1 on every client connection. On 2026-08-07 an
    # exit was handed id 2 -- the id an entry had used two restarts earlier --
    # and store.record_order raised order_id_collision AFTER both legs filled,
    # killing the loop with the pair closed but never written down.
    for name in IBKR_LIVE_ORDER_ENV_GATES:
        monkeypatch.setenv(name, "1")
    outcome = adapter(FakeClient(filled_result(order_id=2))).execute(plan())

    order_id = outcome.orders[0].order_id
    assert order_id != "2"
    assert "PLAN-D1" in order_id
    assert order_id.endswith("-2")           # raw id kept for traceability
    assert outcome.fills[0].order_id == order_id
    assert "PLAN-D1" in outcome.fills[0].fill_id
    # The number IBKR itself uses is still reachable for reconciliation.
    assert outcome.payload["order_id"] == 2


def test_two_plans_reusing_one_ibkr_id_get_distinct_keys(monkeypatch) -> None:
    for name in IBKR_LIVE_ORDER_ENV_GATES:
        monkeypatch.setenv(name, "1")
    entry = adapter(FakeClient(filled_result(order_id=2))).execute(plan())
    second = PairExecutionPlan(
        plan_id="PLAN-D2",
        plan_type=ExecutionPlanType.EXIT,
        direction=Direction.SHORT_UMC_LONG_CCF,
        timestamp=TS,
        row_index=666,
        legs=(umc_leg(),),
        reason="test",
    )
    exit_ = adapter(FakeClient(filled_result(order_id=2))).execute(second)

    assert entry.orders[0].order_id != exit_.orders[0].order_id
    assert entry.fills[0].fill_id != exit_.fills[0].fill_id


def test_no_order_is_sent_with_the_gates_closed(monkeypatch) -> None:
    for name in IBKR_LIVE_ORDER_ENV_GATES:
        monkeypatch.delenv(name, raising=False)
    client = FakeClient(filled_result())

    outcome = adapter(client).execute(plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert client.calls == []
    assert all(name in outcome.message for name in IBKR_LIVE_ORDER_ENV_GATES)


def test_one_open_gate_is_not_enough(monkeypatch) -> None:
    """The venue gate alone must not enable trading; both are required."""
    monkeypatch.delenv("PROJECT_LUX_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.setenv("IBKR_ALLOW_LIVE_ORDER", "1")
    client = FakeClient(filled_result())

    outcome = adapter(client).execute(plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert client.calls == []


def test_gates_are_rechecked_per_order_not_at_construction(monkeypatch) -> None:
    """A long-running process must not keep a permission that was revoked."""
    for name in IBKR_LIVE_ORDER_ENV_GATES:
        monkeypatch.setenv(name, "1")
    client = FakeClient(filled_result())
    engine = adapter(client)

    assert engine.execute(plan()).status == ExecutionOutcomeStatus.FILLED
    monkeypatch.delenv("IBKR_ALLOW_LIVE_ORDER", raising=False)
    assert engine.execute(plan()).status == ExecutionOutcomeStatus.REJECTED
    assert len(client.calls) == 1


# --- whole shares ------------------------------------------------------------


def test_fractional_hedge_is_rounded_toward_zero() -> None:
    """Under-hedging beats over-hedging: an overshoot leaves US exposure the
    CCF leg does not cover."""
    assert whole_share_quantity(406.7) == (406, pytest.approx(0.7))
    assert whole_share_quantity(-406.7) == (406, pytest.approx(0.7))
    assert whole_share_quantity(406.0) == (406, 0.0)


def test_the_order_is_placed_in_whole_shares_and_the_residual_reported(
    gates_open,
) -> None:
    client = FakeClient(filled_result(filled=406.0))

    outcome = adapter(client).execute(plan(umc_leg(quantity=406.73)))

    assert client.calls[0]["quantity"] == 406
    assert outcome.payload["rounding_residual_shares"] == pytest.approx(0.73)
    assert outcome.payload["requested_quantity"] == pytest.approx(406.73)


def test_a_quantity_that_rounds_to_zero_is_refused(gates_open) -> None:
    client = FakeClient(filled_result())

    outcome = adapter(client).execute(plan(umc_leg(quantity=0.4)))

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert client.calls == []


# --- outcome classification --------------------------------------------------


def test_a_filled_order_produces_a_fill(gates_open) -> None:
    client = FakeClient(filled_result())

    outcome = adapter(client).execute(plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.orders[0].status == OrderStatus.FILLED
    assert len(outcome.fills) == 1
    assert outcome.fills[0].quantity == 406.0
    assert outcome.fills[0].price == pytest.approx(18.91)
    assert outcome.recommended_state is None


def test_a_rejected_order_is_FAILED_not_unknown(gates_open) -> None:
    """Terminal, zero filled, position unmoved -- no exposure was created, so
    the coordinator can unwind cleanly instead of pausing."""
    client = FakeClient(
        {
            "classification": "failed",
            "order_id": 8,
            "status": "Cancelled",
            "filled": 0.0,
            "remaining": 406.0,
        }
    )

    outcome = adapter(client).execute(plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.fills == ()


def test_an_ambiguous_outcome_is_UNKNOWN_and_pauses(gates_open) -> None:
    """A timeout or a partial may or may not have moved the position. This is
    the case that must never be reported as a clean failure."""
    client = FakeClient(
        {
            "classification": "unknown",
            "order_id": 9,
            "status": "Submitted",
            "filled": 120.0,
            "remaining": 286.0,
        }
    )

    outcome = adapter(client).execute(plan())

    assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "may or may not have moved" in outcome.message


def test_a_failing_request_is_UNKNOWN_because_the_order_may_have_landed(
    gates_open,
) -> None:
    """The pipe broke. Whether IBKR received the order is unknowable here."""
    client = FakeClient(ConnectionError("worker died"))

    outcome = adapter(client).execute(plan())

    assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "ConnectionError" in outcome.message


# --- leg selection and read-only helpers -------------------------------------


def test_a_plan_without_a_umc_leg_is_rejected(gates_open) -> None:
    ccf_only = ExecutionLeg(
        broker=BrokerName.FUBON_CCF,
        symbol="CCFG6",
        side=OrderSide.BUY,
        quantity=1,
        price=156.0,
        timestamp=TS,
        row_index=42,
    )
    client = FakeClient(filled_result())

    outcome = adapter(client).execute(plan(ccf_only))

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert client.calls == []


def test_it_satisfies_the_position_reader_the_coordinator_guard_needs() -> None:
    """execution/position_guard.py reads positions off the adapters; an adapter
    that cannot answer reads as unverifiable and blocks every order."""
    client = FakeClient(None, position=-406.0)
    engine = adapter(client)

    assert engine.fetch_position_quantity() == -406.0
    assert engine.preflight().position_quantity == -406.0


def test_close_releases_only_a_client_it_owns() -> None:
    client = FakeClient(None)
    adapter(client).close()

    assert client.closed is False
