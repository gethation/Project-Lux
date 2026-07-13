"""M6 execution-channel smoke — SENDS TINY REAL TWO-LEG ORDERS.

Skipped by default. It runs only when every live-order env gate is set AND the
gitignored configs/config.live.exec.smoke.local.toml exists with
allow_live_order=true and [live_execution_smoke] enabled=true.

This test intentionally bypasses live warmup/signal generation. M6 validates the
execution channel only: Fubon TMF 1 lot + Binance TSM 0.1 unit entry, then the
matching exit, with read-only reconciliation after each stage.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from lux_trader.config import AppConfig, load_config
from lux_trader.core.models import BrokerName, Direction, OrderSide, StrategyState
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.core.time import TAIPEI_TZ
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
    make_execution_plan_id,
)
from lux_trader.execution.outcome import ExecutionOutcome
from lux_trader.execution.real_coordinator import RealExecutionCoordinator
from lux_trader.integrations.binance.execution import BinanceTsmExecutionAdapter
from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.fubon.execution import FubonFutureExecutionAdapter
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.reconciliation import ReconciliationStatus
from lux_trader.reconciliation.post_trade import PostTradeReconciler
from lux_trader.store import SQLiteStore


EXEC_SMOKE_CONFIG = Path("configs/config.live.exec.smoke.local.toml")
SMOKE_FUBON_SYMBOL = "TMFG6"
SMOKE_FUBON_LOTS = 1.0
SMOKE_TSM_UNITS = 0.1
SMOKE_DIRECTION = Direction.SHORT_TSM_LONG_QFF

REQUIRED_ENV_GATES = (
    "LUX_READONLY_BROKER",
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "FUBON_ALLOW_LIVE_ORDER",
    "BINANCE_ALLOW_LIVE_ORDER",
)

pytestmark = [
    pytest.mark.readonly_broker,
    pytest.mark.live_execute_smoke,
]


def load_exec_smoke_config() -> AppConfig:
    for gate in REQUIRED_ENV_GATES:
        if os.getenv(gate, "").strip() != "1":
            pytest.skip(f"Set {gate}=1 to run M6 execution smoke (SENDS REAL ORDERS)")
    if not EXEC_SMOKE_CONFIG.exists():
        pytest.skip(f"Exec smoke config does not exist: {EXEC_SMOKE_CONFIG}")
    config = load_config(EXEC_SMOKE_CONFIG)
    if not config.safety.allow_live_order:
        pytest.skip("M6 execution smoke requires allow_live_order=true")
    if not config.live_execution.enabled:
        pytest.skip("M6 execution smoke requires [live_execution] enabled=true")
    if not config.live_execution_smoke.enabled:
        pytest.skip("M6 execution smoke requires [live_execution_smoke] enabled=true")
    if not config.live_execution.qff_first:
        pytest.skip("M6 execution smoke requires live_execution.qff_first=true")

    smoke = config.live_execution_smoke
    assert smoke.fubon_symbol == SMOKE_FUBON_SYMBOL
    assert smoke.fubon_lots == int(SMOKE_FUBON_LOTS)
    assert smoke.tsm_units == pytest.approx(SMOKE_TSM_UNITS)
    assert smoke.binance_symbol == config.live.binance_symbol
    return config


def remove_sqlite_family(path: Path) -> None:
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if target.exists():
            target.unlink()


def print_m6_stage(message: str) -> None:
    print(f"[M6] {message}", flush=True)


def smoke_readonly_brokers(config: AppConfig):
    smoke = config.live_execution_smoke
    return (
        FubonReadOnlyBroker(config.live.fubon_env_path, symbol=smoke.fubon_symbol),
        BinanceReadOnlyBroker(smoke.binance_symbol, config.live.fubon_env_path),
    )


def close_all(resources: tuple[Any, ...]) -> None:
    for resource in resources:
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def smoke_state(config: AppConfig, *, open_position: bool) -> StrategyRuntimeState:
    smoke = config.live_execution_smoke
    if not open_position:
        return StrategyRuntimeState(
            state=StrategyState.FLAT,
            trading_qff_symbol=smoke.fubon_symbol,
            trading_qff_expiry=smoke.qff_expiry,
            contract_policy_state="active",
        )
    return StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=SMOKE_DIRECTION,
        tsm_units=-float(smoke.tsm_units),
        qff_units=float(smoke.fubon_lots),
        qff_contracts=int(smoke.fubon_lots),
        trading_qff_symbol=smoke.fubon_symbol,
        trading_qff_expiry=smoke.qff_expiry,
        contract_policy_state="active",
    )


def record_reconciliation(
    config: AppConfig,
    store: SQLiteStore,
    *,
    state: StrategyRuntimeState,
    label: str,
) -> None:
    brokers = smoke_readonly_brokers(config)
    try:
        report = PostTradeReconciler(
            tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
            qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
        ).reconcile(
            store=store,
            strategy_state=state,
            brokers=brokers,
            tsm_symbol=config.live_execution_smoke.binance_symbol,
            qff_symbol=config.live_execution_smoke.fubon_symbol,
            timestamp=datetime.now(TAIPEI_TZ),
        )
    finally:
        close_all(brokers)
    store.record_reconciliation_report(report)
    store.commit()
    print_m6_stage(
        f"{label} read-only reconciliation status={report.status.value} "
        f"issues={len(report.issues)}"
    )
    assert report.status == ReconciliationStatus.MATCHED, (
        f"{label} reconciliation failed: "
        + str([issue.to_jsonable() if hasattr(issue, "to_jsonable") else str(issue) for issue in report.issues])
    )


def build_smoke_plan(
    config: AppConfig,
    *,
    plan_type: ExecutionPlanType,
    row_index: int,
) -> PairExecutionPlan:
    smoke = config.live_execution_smoke
    timestamp = datetime.now(TAIPEI_TZ)
    if plan_type == ExecutionPlanType.ENTRY:
        fubon_side = OrderSide.BUY
        binance_side = OrderSide.SELL
    else:
        fubon_side = OrderSide.SELL
        binance_side = OrderSide.BUY
    common = {
        "timestamp": timestamp,
        "row_index": row_index,
        "qff_expiry": smoke.qff_expiry,
        "contract_policy_state": "active",
        "order_type": ExecutionOrderType.MARKET.value,
        "expected_price": 1.0,
        "price_source": "m6_execution_smoke_placeholder",
        "raw": {"source": "m6_execution_smoke"},
    }
    legs = (
        ExecutionLeg(
            broker=BrokerName.FUBON_QFF,
            symbol=smoke.fubon_symbol,
            side=fubon_side,
            quantity=float(smoke.fubon_lots),
            price=1.0,
            qff_symbol=smoke.fubon_symbol,
            **common,
        ),
        ExecutionLeg(
            broker=BrokerName.BINANCE_TSM,
            symbol=smoke.binance_symbol,
            side=binance_side,
            quantity=float(smoke.tsm_units),
            price=1.0,
            qff_symbol=smoke.fubon_symbol,
            **common,
        ),
    )
    return PairExecutionPlan(
        plan_id=make_execution_plan_id(
            plan_type=plan_type,
            direction=SMOKE_DIRECTION,
            timestamp=timestamp,
            row_index=row_index,
        ),
        plan_type=plan_type,
        direction=SMOKE_DIRECTION,
        timestamp=timestamp,
        row_index=row_index,
        legs=legs,
        reason=f"m6_execution_smoke_{plan_type.value}",
        qff_symbol=smoke.fubon_symbol,
        qff_expiry=smoke.qff_expiry,
        contract_policy_state="active",
        order_type=ExecutionOrderType.MARKET.value,
        price_policy="m6_execution_smoke_market",
        plan_age_seconds=0.0,
        max_plan_age_seconds=config.live_execution.max_plan_age_seconds,
    )


def execute_smoke_plan(
    store: SQLiteStore,
    coordinator: RealExecutionCoordinator,
    plan: PairExecutionPlan,
) -> ExecutionOutcome:
    _, outcome = coordinator.execute(plan)
    for order in outcome.orders:
        store.record_order(order)
    for fill in outcome.fills:
        store.record_fill(fill)
    store.commit()
    print_m6_stage(
        f"{plan.plan_type.value} execution status={outcome.status.value} "
        f"message={outcome.message}"
    )
    if not outcome.filled:
        print_m6_stage(json.dumps(outcome.to_jsonable(), ensure_ascii=False, default=str))
        pytest.fail(f"{plan.plan_type.value} execution did not fill: {outcome.status.value}")
    return outcome


def dryrun_order_count(connection: sqlite3.Connection) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM orders WHERE order_id LIKE 'DRYRUN-%'"
    ).fetchone()[0]


def latest_plan_legs(
    connection: sqlite3.Connection,
    plan_type: str,
) -> dict[str, dict[str, object]]:
    rows = connection.execute(
        """
        SELECT broker, symbol, side, quantity
        FROM execution_legs
        WHERE plan_id = (
            SELECT plan_id
            FROM execution_plans
            WHERE plan_type = ?
            ORDER BY timestamp DESC, row_index DESC, plan_id DESC
            LIMIT 1
        )
        """,
        (plan_type,),
    ).fetchall()
    return {
        str(row[0]): {
            "symbol": row[1],
            "side": row[2],
            "quantity": float(row[3]),
        }
        for row in rows
    }


def assert_smoke_quantities(
    connection: sqlite3.Connection,
    plan_type: str,
) -> None:
    legs = latest_plan_legs(connection, plan_type)
    fubon_leg = legs["FUBON_QFF"]
    binance_leg = legs["BINANCE_TSM"]
    assert fubon_leg["symbol"] == SMOKE_FUBON_SYMBOL
    assert fubon_leg["quantity"] == pytest.approx(SMOKE_FUBON_LOTS)
    assert binance_leg["quantity"] == pytest.approx(SMOKE_TSM_UNITS)


def latest_outcome_payload(connection: sqlite3.Connection, plan_type: str) -> dict:
    row = connection.execute(
        """
        SELECT outcome.payload_json
        FROM execution_outcomes AS outcome
        JOIN execution_plans AS plan ON plan.plan_id = outcome.plan_id
        WHERE plan.plan_type = ?
        ORDER BY outcome.outcome_id DESC
        LIMIT 1
        """,
        (plan_type,),
    ).fetchone()
    assert row is not None
    return json.loads(row[0])


def assert_and_print_leg_timing_gap(
    connection: sqlite3.Connection,
    plan_type: str,
) -> None:
    outcome = latest_outcome_payload(connection, plan_type)
    gap = (outcome.get("payload") or {}).get("primary_leg_timing_gap")
    assert gap is not None, f"{plan_type} outcome did not record leg timing gap"
    assert gap["submit_start_gap_seconds"] is not None
    print_m6_stage(
        f"{plan_type} leg timing "
        f"submit_start_gap={float(gap['submit_start_gap_seconds']):.3f}s "
        f"submit_handoff_gap={float(gap['submit_handoff_gap_seconds']):.3f}s"
    )


def assert_store_smoke_results(config: AppConfig, *, plan_type: str) -> None:
    connection = sqlite3.connect(config.store_path)
    try:
        assert dryrun_order_count(connection) == 0
        assert_smoke_quantities(connection, plan_type)
        assert_and_print_leg_timing_gap(connection, plan_type)
    finally:
        connection.close()


def test_real_live_execute_entry_and_exit_returns_flat() -> None:
    config = load_exec_smoke_config()
    smoke = config.live_execution_smoke
    print_m6_stage(f"config loaded store_path={config.store_path}")
    print_m6_stage(
        "smoke quantities "
        f"fubon_symbol={smoke.fubon_symbol} "
        f"fubon_lots={smoke.fubon_lots:g} "
        f"tsm_symbol={smoke.binance_symbol} "
        f"tsm_units={smoke.tsm_units:g}"
    )
    remove_sqlite_family(config.store_path)
    print_m6_stage("sqlite store reset")

    store = SQLiteStore(config.store_path)
    binance_adapter = BinanceTsmExecutionAdapter(
        smoke.binance_symbol,
        config.live.fubon_env_path,
        leverage=config.binance_execution.leverage,
        margin_mode=config.binance_execution.margin_mode,
        enforce_leverage=config.binance_execution.enforce_leverage,
    )
    fubon_adapter = FubonFutureExecutionAdapter(
        smoke.fubon_symbol,
        config.live.fubon_env_path,
    )
    try:
        store.initialize()
        record_reconciliation(
            config,
            store,
            state=smoke_state(config, open_position=False),
            label="pre-entry flat",
        )

        coordinator = RealExecutionCoordinator(
            store=store,
            binance_adapter=binance_adapter,
            fubon_adapter=fubon_adapter,
            qff_first=config.live_execution.qff_first,
        )

        entry_plan = build_smoke_plan(
            config,
            plan_type=ExecutionPlanType.ENTRY,
            row_index=0,
        )
        execute_smoke_plan(store, coordinator, entry_plan)
        assert_store_smoke_results(config, plan_type="entry")
        record_reconciliation(
            config,
            store,
            state=smoke_state(config, open_position=True),
            label="post-entry open",
        )

        exit_plan = build_smoke_plan(
            config,
            plan_type=ExecutionPlanType.EXIT,
            row_index=1,
        )
        execute_smoke_plan(store, coordinator, exit_plan)
        assert_store_smoke_results(config, plan_type="exit")
        record_reconciliation(
            config,
            store,
            state=smoke_state(config, open_position=False),
            label="post-exit flat",
        )
    finally:
        close_all((binance_adapter, fubon_adapter))
        store.close()

    print_m6_stage("final broker flat reconciliation passed")
