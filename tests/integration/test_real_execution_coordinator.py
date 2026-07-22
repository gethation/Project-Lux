from __future__ import annotations

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


SYMBOL_TSM = "TSM/USDT:USDT"
SYMBOL_QFF = "QFFG6"


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
    def __init__(self, broker: BrokerName, outcomes: list[dict]) -> None:
        self.broker = broker
        self.outcomes = list(outcomes)
        self.plans: list[PairExecutionPlan] = []

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
                    qff_symbol=leg.qff_symbol,
                    qff_expiry=leg.qff_expiry,
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
        if self.broker == BrokerName.BINANCE_TSM and state.tsm_units:
            positions.append(
                BrokerPositionSnapshot(
                    broker=self.broker,
                    symbol=SYMBOL_TSM,
                    quantity=state.tsm_units,
                )
            )
        if self.broker == BrokerName.FUBON_QFF and state.qff_contracts:
            positions.append(
                BrokerPositionSnapshot(
                    broker=self.broker,
                    symbol=state.trading_qff_symbol or SYMBOL_QFF,
                    quantity=float(state.qff_contracts),
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


def pair_plan(*, qff_quantity: float = 2.0) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id="LIVE-PLAN-1",
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_TSM_LONG_QFF,
        timestamp=ts(),
        row_index=7,
        legs=(
            ExecutionLeg(
                broker=BrokerName.BINANCE_TSM,
                symbol=SYMBOL_TSM,
                side=OrderSide.SELL,
                quantity=100.0,
                price=150.0,
                timestamp=ts(),
                row_index=7,
            ),
            ExecutionLeg(
                broker=BrokerName.FUBON_QFF,
                symbol=SYMBOL_QFF,
                side=OrderSide.BUY,
                quantity=qff_quantity,
                price=1100.0,
                timestamp=ts(),
                row_index=7,
                qff_symbol=SYMBOL_QFF,
            ),
        ),
        reason="test_live_execution",
        qff_symbol=SYMBOL_QFF,
    )


def coordinator(
    store: FakeStore,
    *,
    qff_outcomes: list[dict],
    binance_outcomes: list[dict],
) -> RealExecutionCoordinator:
    return RealExecutionCoordinator(
        store=store,
        fubon_adapter=FakeExecutionAdapter(BrokerName.FUBON_QFF, qff_outcomes),
        binance_adapter=FakeExecutionAdapter(BrokerName.BINANCE_TSM, binance_outcomes),
        qff_first=True,
        clock=ts,
    )


def event_types(store: FakeStore) -> list[str]:
    return [event["event_type"] for event in store.events]


def test_qff_and_binance_full_fill_combines_to_filled() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        qff_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        binance_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
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
        qff_outcomes=[
            {
                "status": ExecutionOutcomeStatus.FILLED,
                "payload": {
                    "submit_started_at": "2026-02-02T09:15:01+08:00",
                    "submit_finished_at": "2026-02-02T09:15:01.250000+08:00",
                },
            }
        ],
        binance_outcomes=[
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
    assert gap["first_broker"] == BrokerName.FUBON_QFF.value
    assert gap["second_broker"] == BrokerName.BINANCE_TSM.value
    assert gap["submit_start_gap_seconds"] == pytest.approx(1.0)
    assert gap["submit_handoff_gap_seconds"] == pytest.approx(0.75)


def test_qff_full_fill_binance_failed_attempts_qff_emergency_close() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        qff_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
        binance_outcomes=[{"status": ExecutionOutcomeStatus.FAILED}],
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
    assert emergency_fill.broker == BrokerName.FUBON_QFF
    assert emergency_fill.side == OrderSide.SELL
    assert emergency_fill.quantity == 2.0


def test_qff_full_fill_binance_failed_and_emergency_close_failed_is_critical() -> None:
    store = FakeStore()
    runner = coordinator(
        store,
        qff_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FAILED},
        ],
        binance_outcomes=[{"status": ExecutionOutcomeStatus.FAILED}],
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "emergency_close_failed" in event_types(store)
    assert "critical_manual_intervention_required" in event_types(store)
    assert (outcome.payload or {})["critical"] is True


def test_qff_partial_fill_does_not_send_binance_and_closes_partial_quantity() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [
            {"status": ExecutionOutcomeStatus.PARTIAL_FILL, "fill_quantity": 1.0},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan(qff_quantity=2.0))

    assert outcome.status == ExecutionOutcomeStatus.PARTIAL_FILL
    assert outcome.recommended_state == StrategyState.PAUSED
    assert len(binance_adapter.plans) == 0
    assert len(qff_adapter.plans) == 2
    assert qff_adapter.plans[1].plan_type == ExecutionPlanType.EXIT
    assert qff_adapter.plans[1].legs[0].quantity == 1.0
    assert qff_adapter.plans[1].legs[0].side == OrderSide.SELL


def test_qff_rejected_zero_fill_does_not_send_binance_or_emergency_close() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [{"status": ExecutionOutcomeStatus.REJECTED}],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert len(qff_adapter.plans) == 1
    assert len(binance_adapter.plans) == 0
    assert store.events == []


def test_binance_partial_after_qff_full_fill_unwinds_both_filled_legs() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [
            {"status": ExecutionOutcomeStatus.PARTIAL_FILL, "fill_quantity": 40.0},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.PARTIAL_FILL
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "imbalanced_pair_exposure" in event_types(store)
    assert len(qff_adapter.plans) == 2
    assert len(binance_adapter.plans) == 2
    assert qff_adapter.plans[1].legs[0].side == OrderSide.SELL
    assert binance_adapter.plans[1].legs[0].side == OrderSide.BUY
    assert binance_adapter.plans[1].legs[0].quantity == 40.0


def test_qff_first_false_rejects_without_calling_adapters() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=False,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert len(qff_adapter.plans) == 0
    assert len(binance_adapter.plans) == 0
    assert store.plans[0].status.value == "rejected"


def test_qff_adapter_exception_stops_without_binance_and_pauses() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [{"raise": True, "message": "fubon sdk boom"}],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [{"status": ExecutionOutcomeStatus.FILLED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=True,
        clock=ts,
    )

    # A raising QFF adapter must be contained (no propagating exception) and pause.
    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert len(qff_adapter.plans) == 1
    assert len(binance_adapter.plans) == 0  # second leg must not be sent
    assert store.events == []  # zero known QFF fill => no auto emergency close
    primary = (outcome.payload or {})["primary_outcomes"]
    assert "adapter raised" in primary[BrokerName.FUBON_QFF.value]["message"]


def test_binance_adapter_exception_after_qff_fill_emergency_closes_qff() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},  # emergency close
        ],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [{"raise": True, "message": "binance sdk boom"}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=True,
        clock=ts,
    )

    _, outcome = runner.execute(pair_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "single_leg_exposure" in event_types(store)
    assert "emergency_close_filled" in event_types(store)
    assert (outcome.payload or {})["critical"] is False
    assert len(qff_adapter.plans) == 2  # primary + emergency close
    assert qff_adapter.plans[1].legs[0].side == OrderSide.SELL
    assert len(binance_adapter.plans) == 1  # raised once, not retried


def test_emergency_close_adapter_exception_is_critical_without_crash() -> None:
    store = FakeStore()
    qff_adapter = FakeExecutionAdapter(
        BrokerName.FUBON_QFF,
        [
            {"status": ExecutionOutcomeStatus.FILLED},
            {"raise": True, "message": "emergency close boom"},
        ],
    )
    binance_adapter = FakeExecutionAdapter(
        BrokerName.BINANCE_TSM,
        [{"status": ExecutionOutcomeStatus.FAILED}],
    )
    runner = RealExecutionCoordinator(
        store=store,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        qff_first=True,
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
        qff_close=1000.0,
        qff_close_filled=1000.0,
        tsm_twd_fair=1100.0,
        spread=9.5,
        entry_allowed=True,
        close_allowed=True,
        qff_symbol=SYMBOL_QFF,
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
        candidate_direction=Direction.SHORT_TSM_LONG_QFF,
        candidate_idx=7,
        candidate_time=ts(),
        candidate_zscore=2.5,
    )
    return PairStrategy(
        config.strategy,
        config.fees,
        PaperBroker(),
        state=state,
        tsm_symbol=SYMBOL_TSM,
    )


def test_live_entry_success_applies_strategy_open_position(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        qff_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        binance_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
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
    assert strategy.state.position_direction == Direction.SHORT_TSM_LONG_QFF
    tsm_leg = next(leg for leg in plan.legs if leg.broker == BrokerName.BINANCE_TSM)
    assert tsm_leg.quantity == pytest.approx(1_000_000.0 / (1100.0 * 5.0))
    assert strategy.state.tsm_units == pytest.approx(-1_000_000.0 / (1100.0 * 5.0))


def test_live_entry_uses_actual_fills_for_state_and_exit_quantity(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        qff_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}
        ],
        binance_outcomes=[
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
    assert strategy.state.tsm_units == -909.0
    assert strategy.state.qff_contracts == 10
    assert strategy.state.qff_units == 1000.0
    assert strategy.state.actual_leg_notional_twd == 1_000_000.0

    costs = fill_costs(
        tsm_units=strategy.state.tsm_units,
        tsm_price=live_bar().tsm_twd_fair,
        qff_contracts=strategy.state.qff_contracts,
        qff_price=live_bar().qff_close_filled,
        fees=strategy.fees,
    )
    exit_requests = strategy.build_exit_order_requests(
        bar=live_bar(),
        costs=costs,
    )
    tsm_exit = next(
        request
        for request in exit_requests
        if request.broker == BrokerName.BINANCE_TSM
    )
    qff_exit = next(
        request
        for request in exit_requests
        if request.broker == BrokerName.FUBON_QFF
    )
    assert tsm_exit.side == OrderSide.BUY
    assert tsm_exit.quantity == 909.0
    assert qff_exit.side == OrderSide.SELL
    assert qff_exit.quantity == 10


def test_live_entry_pauses_when_filled_outcome_is_missing_a_leg_fill(tmp_path) -> None:
    store = FakeStore()
    strategy = entry_pending_strategy(tmp_path)
    runner = coordinator(
        store,
        qff_outcomes=[{"status": ExecutionOutcomeStatus.FILLED}],
        binance_outcomes=[
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
        qff_outcomes=[
            {"status": ExecutionOutcomeStatus.FILLED},
            {"status": ExecutionOutcomeStatus.FILLED},
        ],
        binance_outcomes=[{"status": ExecutionOutcomeStatus.FAILED}],
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
    strategy.state.trading_qff_symbol = SYMBOL_QFF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_QFF,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}],
        ),
        binance_adapter=FakeExecutionAdapter(
            BrokerName.BINANCE_TSM,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}],
        ),
        readonly_brokers=(
            StrategyReadOnlyBroker(BrokerName.FUBON_QFF, strategy),
            StrategyReadOnlyBroker(BrokerName.BINANCE_TSM, strategy),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, qff_symbol=SYMBOL_QFF, qff_expiry=None)

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
            qff_symbol=SYMBOL_QFF,
            qff_expiry=None,
        )

        report = store.load_latest_reconciliation_report()
        assert mode_result.result.action == StrategyAction.ENTRY_FILL
        assert strategy.state.state == StrategyState.OPEN
        assert strategy.state.tsm_units == -909.0
        assert strategy.state.qff_contracts == 10
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
    strategy.state.trading_qff_symbol = SYMBOL_QFF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_QFF,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}],
        ),
        binance_adapter=FakeExecutionAdapter(
            BrokerName.BINANCE_TSM,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}],
        ),
        readonly_brokers=(
            StrategyReadOnlyBroker(BrokerName.FUBON_QFF, strategy),
            StrategyReadOnlyBroker(BrokerName.BINANCE_TSM, strategy),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, qff_symbol=SYMBOL_QFF, qff_expiry=None)

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
            qff_symbol=SYMBOL_QFF,
            qff_expiry=None,
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
        broker = BrokerName.FUBON_QFF

        def fetch_snapshot(self):
            raise TimeoutError("Fubon snapshot timeout")

        def close(self) -> None:
            return None

    config = make_app_config(tmp_path)
    store = SQLiteStore(config.store_path)
    strategy = entry_pending_strategy(tmp_path)
    strategy.state.trading_qff_symbol = SYMBOL_QFF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_QFF,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 10.0}],
        ),
        binance_adapter=FakeExecutionAdapter(
            BrokerName.BINANCE_TSM,
            [{"status": ExecutionOutcomeStatus.FILLED, "fill_quantity": 909.0}],
        ),
        readonly_brokers=(
            RaisingReadOnlyBroker(),
            StrategyReadOnlyBroker(BrokerName.BINANCE_TSM, strategy),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, qff_symbol=SYMBOL_QFF, qff_expiry=None)

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
            qff_symbol=SYMBOL_QFF,
            qff_expiry=None,
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
    strategy.state.trading_qff_symbol = SYMBOL_QFF
    handler = LiveExecuteModeHandler(
        config,
        fubon_adapter=FakeExecutionAdapter(
            BrokerName.FUBON_QFF,
            [{"status": ExecutionOutcomeStatus.FILLED}],
        ),
        binance_adapter=FakeExecutionAdapter(
            BrokerName.BINANCE_TSM,
            [{"status": ExecutionOutcomeStatus.FILLED}],
        ),
        readonly_brokers=(
            FakeReadOnlyBroker(BrokerName.FUBON_QFF, fetched_at=ts()),
            FakeReadOnlyBroker(BrokerName.BINANCE_TSM, fetched_at=ts()),
        ),
    )
    try:
        store.initialize()
        handler.on_runtime_ready(store, qff_symbol=SYMBOL_QFF, qff_expiry=None)

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
            qff_symbol=SYMBOL_QFF,
            qff_expiry=None,
        )

        report = store.load_latest_reconciliation_report()
        assert mode_result.result.action == StrategyAction.LIVE_EXECUTION
        assert mode_result.result.reason == "post_trade_reconciliation_mismatch"
        assert strategy.state.state == StrategyState.PAUSED
        assert strategy.state.position_direction == Direction.SHORT_TSM_LONG_QFF
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
