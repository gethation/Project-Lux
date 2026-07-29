from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from lux_trader.execution import (
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    order_request_from_execution_leg,
)
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.core.models import (
    BrokerName,
    Direction,
    Fill,
    IndicatorSnapshot,
    MarketBar,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyAction,
    StrategyState,
)
from lux_trader.brokers import PaperBroker
from lux_trader.core.fees import fill_costs
from lux_trader.runtime.live.modes import (
    LiveExecuteModeHandler,
    execute_live_entry,
)
from lux_trader.reconciliation import (
    BrokerAccountSnapshot,
    BrokerPositionSnapshot,
    FakeReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.store import SQLiteStore
from lux_trader.execution.real_coordinator import RealExecutionCoordinator
from lux_trader.core.strategy import PairStrategy, StrategyRuntimeState
from lux_trader.terminal_ui import NullLiveReporter
from conftest import make_app_config


SYMBOL_UMC = "UMC"
SYMBOL_CCF = "CCFG6"


class FakeStore:
    def __init__(self) -> None:
        self.plans: list[PairExecutionPlan] = []
        self.outcomes: list[ExecutionOutcome] = []
        self.events: list[dict] = []

    def record_execution_plan(self, plan: PairExecutionPlan) -> None:
        self.plans.append(plan)

    def record_execution_outcome(self, outcome: ExecutionOutcome) -> int:
        self.outcomes.append(outcome)
        return len(self.outcomes)

    def record_event(
        self,
        row_index: int,
        timestamp: datetime,
        event_type: str,
        message: str,
        payload: dict | None = None,
    ) -> None:
        self.events.append(
            {
                "row_index": row_index,
                "timestamp": timestamp,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
            }
        )


class FakeExecutionAdapter:
    def __init__(
        self,
        broker: BrokerName,
        outcomes: list[dict],
        *,
        position_quantity: float = 0.0,
    ) -> None:
        self.broker = broker
        self.outcomes = list(outcomes)
        self.plans: list[PairExecutionPlan] = []
        # The coordinator asks before sending anything; flat is what an entry
        # plan expects to find. See execution/position_guard.py.
        self.position_quantity = float(position_quantity)

    def fetch_position_quantity(self) -> float:
        return self.position_quantity

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        self.plans.append(plan)
        spec = self.outcomes.pop(0)
        if spec.get("raise"):
            raise RuntimeError(spec.get("message", "adapter boom"))
        leg = plan.legs[0]
        status = spec["status"]
        fill_quantity = spec.get("fill_quantity")
        if fill_quantity is None and status == ExecutionOutcomeStatus.FILLED:
            fill_quantity = leg.quantity
        order = OrderResult(
            order_id=f"{self.broker.value}-{len(self.plans)}",
            request=order_request_from_execution_leg(leg),
            status=(
                OrderStatus.FILLED
                if status == ExecutionOutcomeStatus.FILLED
                else OrderStatus.OPEN
            ),
        )
        fills = ()
        if fill_quantity:
            fills = (
                Fill(
                    fill_id=f"FILL-{self.broker.value}-{len(self.plans)}",
                    order_id=order.order_id,
                    broker=leg.broker,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=float(fill_quantity),
                    price=leg.expected_price or leg.price,
                    fee_twd=leg.fee_twd,
                    timestamp=leg.timestamp,
                    row_index=leg.row_index,
                    ccf_symbol=leg.ccf_symbol,
                    ccf_expiry=leg.ccf_expiry,
                    contract_policy_state=leg.contract_policy_state,
                ),
            )
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=status,
            message=spec.get("message", status.value),
            orders=(order,),
            fills=fills,
            recommended_state=(
                None
                if status == ExecutionOutcomeStatus.FILLED
                else StrategyState.PAUSED
            ),
            payload={"adapter": self.broker.value, **spec.get("payload", {})},
        )


class StrategyReadOnlyBroker:
    def __init__(self, broker: BrokerName, strategy: PairStrategy) -> None:
        self.broker = broker
        self.strategy = strategy

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        positions = []
        state = self.strategy.state
        if self.broker == BrokerName.IBKR_UMC and state.umc_units:
            positions.append(
                BrokerPositionSnapshot(
                    broker=self.broker,
                    symbol=SYMBOL_UMC,
                    quantity=state.umc_units,
                )
            )
        if self.broker == BrokerName.FUBON_CCF and state.ccf_contracts:
            positions.append(
                BrokerPositionSnapshot(
                    broker=self.broker,
                    symbol=state.trading_ccf_symbol or SYMBOL_CCF,
                    quantity=float(state.ccf_contracts),
                )
            )
        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id=f"{self.broker.value}-ACCOUNT",
            fetched_at=ts(),
            positions=tuple(positions),
        )

    def close(self) -> None:
        return None


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


def pair_plan(*, ccf_quantity: float = 2.0) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id="LIVE-PLAN-1",
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_UMC_LONG_CCF,
        timestamp=ts(),
        row_index=7,
        legs=(
            ExecutionLeg(
                broker=BrokerName.IBKR_UMC,
                symbol=SYMBOL_UMC,
                side=OrderSide.SELL,
                quantity=100.0,
                price=150.0,
                timestamp=ts(),
                row_index=7,
            ),
            ExecutionLeg(
                broker=BrokerName.FUBON_CCF,
                symbol=SYMBOL_CCF,
                side=OrderSide.BUY,
                quantity=ccf_quantity,
                price=1100.0,
                timestamp=ts(),
                row_index=7,
                ccf_symbol=SYMBOL_CCF,
            ),
        ),
        reason="test_live_execution",
        ccf_symbol=SYMBOL_CCF,
    )


def exit_plan(*, ccf_quantity: float = 2.0) -> PairExecutionPlan:
    """Closing SHORT_UMC_LONG_CCF: buy back the UMC short, sell the CCF long."""
    return replace(
        pair_plan(ccf_quantity=ccf_quantity),
        plan_id="LIVE-EXIT-1",
        plan_type=ExecutionPlanType.EXIT,
        legs=(
            replace(pair_plan().legs[0], side=OrderSide.BUY),
            replace(pair_plan(ccf_quantity=ccf_quantity).legs[1], side=OrderSide.SELL),
        ),
    )


def coordinator(
    store: FakeStore,
    *,
    ccf_outcomes: list[dict],
    umc_outcomes: list[dict],
    ccf_position: float = 0.0,
    umc_position: float = 0.0,
) -> RealExecutionCoordinator:
    return RealExecutionCoordinator(
        store=store,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_CCF, ccf_outcomes, position_quantity=ccf_position
        ),
        umc_adapter=FakeExecutionAdapter(
            BrokerName.IBKR_UMC, umc_outcomes, position_quantity=umc_position
        ),
        ccf_first=True,
        clock=ts,
    )


def event_types(store: FakeStore) -> list[str]:
    return [event["event_type"] for event in store.events]


def test_ccf_and_umc_full_fill_combines_to_filled() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        ccf_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
    )

    recorded, outcome = runner.execute(pair_plan())

    assert recorded.status.value == "recorded"
    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.recommended_state is None
    assert len(outcome.orders) == 2
    assert len(outcome.fills) == 2
    assert store.events == []
    assert store.outcomes == [outcome]


def test_pair_fill_records_primary_leg_timing_gap() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        ccf_outcomes=[
            {
                "status": ExecutionOutcomeStatus.FILLED,
                "payload": {
                    "submit_started_at": "2026-02-02T09:15:01+08:00",
                    "submit_finished_at": "2026-02-02T09:15:01.250000+08:00",
                },
            }
        ],
        umc_outcomes=[
            {
                "status": ExecutionOutcomeStatus.FILLED,
                "payload": {
                    "submit_started_at": "2026-02-02T09:15:02+08:00",
                    "submit_finished_at": "2026-02-02T09:15:02.100000+08:00",
                },
            }
        ],
    )

    _, outcome = runner.execute(pair_plan())

    gap = (outcome.payload or {})["primary_leg_timing_gap"]
    assert gap["first_broker"] == BrokerName.FUBON_CCF.value
    assert gap["second_broker"] == BrokerName.IBKR_UMC.value
    assert gap["submit_start_gap_seconds"] == pytest.approx(1.0)
    assert gap["submit_handoff_gap_seconds"] == pytest.approx(0.75)


def test_ccf_full_fill_umc_failed_attempts_ccf_emergency_close() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        ccf_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FAILED}],
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert event_types(store) == [
        "exposure_breach",
        "single_leg_exposure",
        "emergency_close_attempted",
        "emergency_close_filled",
    ]
    payload = outcome.payload or {}
    assert payload["events"][1]["event_type"] == "single_leg_exposure"
    assert payload["critical"] is False
    emergency_fill = outcome.fills[-1]
    assert emergency_fill.broker == BrokerName.FUBON_CCF
    assert emergency_fill.side == OrderSide.SELL
    assert emergency_fill.quantity == 2.0


def test_ccf_full_fill_umc_failed_and_emergency_close_failed_is_critical() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        ccf_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FAILED},
        ],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FAILED}],
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "emergency_close_failed" in event_types(store)
    assert "critical_manual_intervention_required" in event_types(store)
    assert (outcome.payload or {})["critical"] is True


def test_ccf_partial_fill_does_not_send_umc_and_closes_partial_quantity() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [
            {"status": ExecutionOutcomeStatus.PARTIAL_FILL, "fill_quantity": 1.0},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan(ccf_quantity=2.0))

    assert outcome.status == ExecutionOutcomeStatus.PARTIAL_FILL
    assert outcome.recommended_state == StrategyState.PAUSED
    assert len(umc_adapter.plans) == 0
    assert len(ccf_adapter.plans) == 2
    assert ccf_adapter.plans[1].plan_type == ExecutionPlanType.EXIT
    assert ccf_adapter.plans[1].legs[0].quantity == 1.0
    assert ccf_adapter.plans[1].legs[0].side == OrderSide.SELL


def test_ccf_rejected_zero_fill_does_not_send_umc_or_emergency_close() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [{"status": ExecutionOutcomeStatus.REJECTED}],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert len(ccf_adapter.plans) == 1
    assert len(umc_adapter.plans) == 0
    assert store.events == []


def test_umc_partial_after_ccf_full_fill_unwinds_both_filled_legs() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [
            {"status": ExecutionOutcomeStatus.PARTIAL_FILL, "fill_quantity": 40.0},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.PARTIAL_FILL
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "imbalanced_pair_exposure" in event_types(store)
    assert len(ccf_adapter.plans) == 2
    assert len(umc_adapter.plans) == 2
    assert ccf_adapter.plans[1].legs[0].side == OrderSide.SELL
    assert umc_adapter.plans[1].legs[0].side == OrderSide.BUY
    assert umc_adapter.plans[1].legs[0].quantity == 40.0


def test_ccf_first_false_rejects_without_calling_adapters() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=False,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert len(ccf_adapter.plans) == 0
    assert len(umc_adapter.plans) == 0
    assert store.plans[0].status.value == "rejected"


def test_ccf_adapter_exception_stops_without_umc_and_pauses() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [{"raise": True, "message": "fubon sdk boom"}],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=True,
        clock=ts,
    )

    # A raising CCF adapter must be contained (no propagating exception) and pause.
    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert len(ccf_adapter.plans) == 1
    assert len(umc_adapter.plans) == 0  # second leg must not be sent
    assert store.events == []  # zero known CCF fill => no auto emergency close
    primary = (outcome.payload or {})["primary_outcomes"]
    assert "adapter raised" in primary[BrokerName.FUBON_CCF.value]["message"]


def test_umc_adapter_exception_after_ccf_fill_emergency_closes_ccf() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},  # emergency close
        ],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [{"raise": True, "message": "ibkr sdk boom"}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "single_leg_exposure" in event_types(store)
    assert "emergency_close_filled" in event_types(store)
    assert (outcome.payload or {})["critical"] is False
    assert len(ccf_adapter.plans) == 2  # primary + emergency close
    assert ccf_adapter.plans[1].legs[0].side == OrderSide.SELL
    assert len(umc_adapter.plans) == 1  # raised once, not retried


def test_emergency_close_adapter_exception_is_critical_without_crash() -> None:
    store = FakeStore()
    ccf_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_CCF,
        [
            {"status": ExecutionOutcomeStatus.FILLED},
            {"raise": True, "message": "emergency close boom"},
        ],
    )
    umc_adapter = FakeExecutionAdapter(
        BrokerName.IBKR_UMC,
        [{"status": ExecutionOutcomeStatus.FAILED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        ccf_first=True,
        clock=ts,
    )

    # Even the emergency close adapter raising must not crash; it must escalate.
    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "emergency_close_failed" in event_types(store)
    assert "critical_manual_intervention_required" in event_types(store)
    assert (outcome.payload or {})["critical"] is True


def live_bar() -> MarketBar:
    return MarketBar(
        row_index=7,
        timestamp=ts(),
        ccf_close=1000.0,
        ccf_close_filled=1000.0,
        umc_twd_fair=1100.0,
        spread=9.5,
        entry_allowed=True,
        close_allowed=True,
        ccf_symbol=SYMBOL_CCF,
    )


def live_snapshot() -> IndicatorSnapshot:
    return IndicatorSnapshot(
        timestamp=ts(),
        spread=9.5,
        mean=0.0,
        std=1.0,
        zscore=2.5,
        zscore_valid=True,
        entry_allowed=True,
        close_allowed=True,
        friday_night_close_only=False,
    )


def entry_pending_strategy(tmp_path) -> PairStrategy:
    config = make_app_config(tmp_path)
    state = StrategyRuntimeState(
        state=StrategyState.ENTRY_PENDING,
        candidate_direction=Direction.SHORT_UMC_LONG_CCF,
        candidate_idx=7,
        candidate_time=ts(),
        candidate_zscore=2.5,
    )
    return PairStrategy(
        config.strategy,
        config.fees,
        PaperBroker(),
        state=state,
        umc_symbol=SYMBOL_UMC,
    )


def test_live_entry_success_applies_strategy_open_position(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        ccf_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
    )

    result, plan, outcome = execute_live_entry(
        strategy,
        runner,
        live_bar(),
        live_snapshot(),
        "shortSpread",
        None,
        120,
    )

    assert plan is not None
    assert outcome is not None and outcome.filled
    assert result.action == StrategyAction.ENTRY_FILL
    assert strategy.state.state == StrategyState.OPEN
    assert strategy.state.position_direction == Direction.SHORT_UMC_LONG_CCF
    umc_leg = next(leg for leg in plan.legs if leg.broker == BrokerName.IBKR_UMC)
    assert umc_leg.quantity == pytest.approx(1_000_000.0 / (1100.0 * 5.0))
    assert strategy.state.umc_units == pytest.approx(-1_000_000.0 / (1100.0 * 5.0))


def test_live_entry_uses_actual_fills_for_state_and_exit_quantity(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        ccf_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}
        ],
        umc_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}
        ],
    )

    result, _, outcome = execute_live_entry(
        strategy,
        runner,
        live_bar(),
        live_snapshot(),
        "shortSpread",
        None,
        120,
    )

    assert outcome is not None and outcome.filled
    assert result.action == StrategyAction.ENTRY_FILL
    assert strategy.state.umc_units == -909.0
    assert strategy.state.ccf_contracts == 10
    assert strategy.state.ccf_units == 1000.0
    assert strategy.state.actual_leg_notional_twd == 1_000_000.0

    costs = fill_costs(
        umc_units=strategy.state.umc_units,
        umc_price=live_bar().umc_twd_fair,
        ccf_contracts=strategy.state.ccf_contracts,
        ccf_price=live_bar().ccf_close_filled,
        fees=strategy.fees,
    )
    exit_requests = strategy.build_exit_order_requests(
        bar=live_bar(),
        costs=costs,
    )
    umc_exit = next(
        request
        for request in exit_requests
        if request.broker == BrokerName.IBKR_UMC
    )
    ccf_exit = next(
        request
        for request in exit_requests
        if request.broker == BrokerName.FUBON_CCF
    )
    assert umc_exit.side == OrderSide.BUY
    assert umc_exit.quantity == 909.0
    assert ccf_exit.side == OrderSide.SELL
    assert ccf_exit.quantity == 10


def test_live_entry_pauses_when_filled_outcome_is_missing_a_leg_fill(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        ccf_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        umc_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 0.0}
        ],
    )

    result, _, outcome = execute_live_entry(
        strategy,
        runner,
        live_bar(),
        live_snapshot(),
        "shortSpread",
        None,
        120,
    )

    assert outcome is not None and outcome.filled
    assert result.action == StrategyAction.LIVE_EXECUTION
    assert result.reason == "live_entry_fill_mismatch"
    assert strategy.state.state == StrategyState.PAUSED
    assert strategy.state.position_direction is None


def test_live_entry_breach_pauses_without_creating_strategy_position(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        ccf_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FAILED}],
    )

    result, plan, outcome = execute_live_entry(
        strategy,
        runner,
        live_bar(),
        live_snapshot(),
        "shortSpread",
        None,
        120,
    )

    assert plan is not None
    assert outcome is not None
    assert not outcome.filled
    assert result.action == StrategyAction.LIVE_EXECUTION
    assert strategy.state.state == StrategyState.PAUSED
    assert strategy.state.position_direction is None
    assert result.trade is None
    assert "exposure_breach" in event_types(store)


def test_live_execute_post_trade_reconciliation_match_keeps_open_state(tmp_path) -> None:
    config = make_app_config(tmp_path)
    store = SQLiteStore(config.store_path)
    strategy = entry_pending_strategy(tmp_path)
    strategy.state.trading_ccf_symbol = SYMBOL_CCF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_CCF,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}],
        ),
        umc_adapter=FakeExecutionAdapter(
            BrokerName.IBKR_UMC,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}],
        ),
        readonly_brokers=(
            StrategyReadOnlyBroker(BrokerName.FUBON_CCF, strategy),
            StrategyReadOnlyBroker(BrokerName.IBKR_UMC, strategy),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, ccf_symbol=SYMBOL_CCF, ccf_expiry=None)

        mode_result = handler.handle_bar(
            config=config,
            store=store,
            reporter=NullLiveReporter(),
            strategy=strategy,
            bar=live_bar(),
            decision_snapshot=live_snapshot(),
            decision_spread_type="shortSpread",
            quote_set=None,
            force_exit_reason=None,
            ccf_symbol=SYMBOL_CCF,
            ccf_expiry=None,
        )

        report = store.load_latest_reconciliation_report()
        assert mode_result.result.action == StrategyAction.ENTRY_FILL
        assert strategy.state.state == StrategyState.OPEN
        assert strategy.state.umc_units == -909.0
        assert strategy.state.ccf_contracts == 10
        assert report is not None
        assert report.status == ReconciliationStatus.MATCHED
    finally:
        handler.close()
        store.close()


def test_live_execute_ledger_only_mismatch_does_not_pause(tmp_path) -> None:
    class MissingFillStore(SQLiteStore):
        def record_fill(self, fill) -> None:
            # Simulate the historical Fubon ledger-loss incident while broker
            # positions and strategy state remain correct.
            return None

    config = make_app_config(tmp_path)
    store = MissingFillStore(config.store_path)
    strategy = entry_pending_strategy(tmp_path)
    strategy.state.trading_ccf_symbol = SYMBOL_CCF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_CCF,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}],
        ),
        umc_adapter=FakeExecutionAdapter(
            BrokerName.IBKR_UMC,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}],
        ),
        readonly_brokers=(
            StrategyReadOnlyBroker(BrokerName.FUBON_CCF, strategy),
            StrategyReadOnlyBroker(BrokerName.IBKR_UMC, strategy),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, ccf_symbol=SYMBOL_CCF, ccf_expiry=None)

        mode_result = handler.handle_bar(
            config=config,
            store=store,
            reporter=NullLiveReporter(),
            strategy=strategy,
            bar=live_bar(),
            decision_snapshot=live_snapshot(),
            decision_spread_type="shortSpread",
            quote_set=None,
            force_exit_reason=None,
            ccf_symbol=SYMBOL_CCF,
            ccf_expiry=None,
        )

        report = store.load_latest_reconciliation_report()
        assert mode_result.result.action == StrategyAction.ENTRY_FILL
        assert strategy.state.state == StrategyState.OPEN
        assert handler._last_reconciliation_requires_pause is False
        assert report is not None
        assert {
            issue.issue_type for issue in report.issues
        } == {"recorded_fill_position_mismatch"}
    finally:
        handler.close()
        store.close()


def test_live_execute_query_failure_closes_entry_gate_without_pausing(tmp_path) -> None:
    class RaisingReadOnlyBroker:
        broker = BrokerName.FUBON_CCF

        def fetch_snapshot(self):
            raise TimeoutError("Fubon snapshot timeout")

        def close(self) -> None:
            return None

    config = make_app_config(tmp_path)
    store = SQLiteStore(config.store_path)
    strategy = entry_pending_strategy(tmp_path)
    strategy.state.trading_ccf_symbol = SYMBOL_CCF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_CCF,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}],
        ),
        umc_adapter=FakeExecutionAdapter(
            BrokerName.IBKR_UMC,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}],
        ),
        readonly_brokers=(
            RaisingReadOnlyBroker(),
            StrategyReadOnlyBroker(BrokerName.IBKR_UMC, strategy),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, ccf_symbol=SYMBOL_CCF, ccf_expiry=None)

        handler.handle_bar(
            config=config,
            store=store,
            reporter=NullLiveReporter(),
            strategy=strategy,
            bar=live_bar(),
            decision_snapshot=live_snapshot(),
            decision_spread_type="shortSpread",
            quote_set=None,
            force_exit_reason=None,
            ccf_symbol=SYMBOL_CCF,
            ccf_expiry=None,
        )

        assert strategy.state.state == StrategyState.OPEN
        assert handler.reconciliation_entry_blocked is True
        assert handler._last_reconciliation_requires_pause is False
    finally:
        handler.close()
        store.close()


def test_live_execute_post_trade_reconciliation_mismatch_pauses_strategy(
    tmp_path,
) -> None:
    config = make_app_config(tmp_path)
    store = SQLiteStore(config.store_path)
    strategy = entry_pending_strategy(tmp_path)
    strategy.state.trading_ccf_symbol = SYMBOL_CCF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_CCF,
            [{"status": ExecutionOutcomeStatus.FILLED}],
        ),
        umc_adapter=FakeExecutionAdapter(
            BrokerName.IBKR_UMC,
            [{"status": ExecutionOutcomeStatus.FILLED}],
        ),
        readonly_brokers=(
            FakeReadOnlyBroker(BrokerName.FUBON_CCF, fetched_at=ts()),
            FakeReadOnlyBroker(BrokerName.IBKR_UMC, fetched_at=ts()),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, ccf_symbol=SYMBOL_CCF, ccf_expiry=None)

        mode_result = handler.handle_bar(
            config=config,
            store=store,
            reporter=NullLiveReporter(),
            strategy=strategy,
            bar=live_bar(),
            decision_snapshot=live_snapshot(),
            decision_spread_type="shortSpread",
            quote_set=None,
            force_exit_reason=None,
            ccf_symbol=SYMBOL_CCF,
            ccf_expiry=None,
        )

        report = store.load_latest_reconciliation_report()
        assert mode_result.result.action == StrategyAction.LIVE_EXECUTION
        assert mode_result.result.reason == "post_trade_reconciliation_mismatch"
        assert strategy.state.state == StrategyState.PAUSED
        assert strategy.state.position_direction == Direction.SHORT_UMC_LONG_CCF
        assert report is not None
        assert report.status == ReconciliationStatus.WARNING
        assert any(
            issue.issue_type == "position_quantity_mismatch"
            for issue in report.issues
        )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM broker_reconciliation_runs"
        ).fetchone()[0] == 2
    finally:
        handler.close()
        store.close()


def test_a_recalled_umc_short_is_refused_before_any_order_is_sent() -> None:
    """The recall scenario, end to end through the coordinator.

    IBKR reports flat because the short was bought in without our consent. The
    exit plan's BUY would open a fresh long on top of the CCF long. Nothing may
    reach either adapter, including the Fubon leg that would otherwise fill
    first and leave a naked position behind.
    """
    store = FakeStore()
    engine = coordinator(
        store,
        ccf_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        ccf_position=2.0,
        umc_position=0.0,  # recalled
    )

    recorded, outcome = engine.execute(exit_plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert engine.adapters[BrokerName.FUBON_CCF].plans == []
    assert engine.adapters[BrokerName.IBKR_UMC].plans == []

    guard = outcome.payload["position_guard"]
    assert guard["passed"] is False
    failed = [check for check in guard["checks"] if not check["passed"]]
    assert [check["broker"] for check in failed] == [BrokerName.IBKR_UMC.value]
    assert "would OPEN a position" in failed[0]["detail"]


def test_a_matching_exit_still_goes_through() -> None:
    """The guard must not become a blanket refusal of exits."""
    store = FakeStore()
    engine = coordinator(
        store,
        ccf_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        umc_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        ccf_position=2.0,
        umc_position=-100.0,
    )

    _, outcome = engine.execute(exit_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert len(engine.adapters[BrokerName.FUBON_CCF].plans) == 1
    assert len(engine.adapters[BrokerName.IBKR_UMC].plans) == 1
