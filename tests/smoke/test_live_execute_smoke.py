"""M6 minimal live-execute acceptance — SENDS TINY REAL TWO-LEG ORDERS.

Skipped by default. It runs only when every live-order env gate is set AND the
gitignored configs/config.live.exec.smoke.local.toml exists with
allow_live_order=true. Run it attended, with minimal sizing (~1 QFF lot). See
docs/M6_LIVE_EXECUTE_ACCEPTANCE.md for the full runbook and the manual recovery
steps if it stops with an open position.
"""
from __future__ import annotations

import io
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from lux_trader.config import AppConfig, load_config
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.models import Direction, StrategyState
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.core.time import TAIPEI_TZ
from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.market_data import floor_minute
from lux_trader.reconciliation import BrokerReconciler, ReconciliationStatus
from lux_trader.runtime.live import LiveExecuteRunner
from lux_trader.store import SQLiteStore
from lux_trader.terminal_ui import LiveTerminalReporter


EXEC_SMOKE_CONFIG = Path("configs/config.live.exec.smoke.local.toml")

REQUIRED_ENV_GATES = (
    "LUX_LIVE_MARKETDATA",
    "LUX_READONLY_BROKER",
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "FUBON_ALLOW_LIVE_ORDER",
    "BINANCE_ALLOW_LIVE_ORDER",
)

pytestmark = [
    pytest.mark.live_marketdata,
    pytest.mark.readonly_broker,
    pytest.mark.live_execute_smoke,
]


def load_exec_smoke_config() -> AppConfig:
    for gate in REQUIRED_ENV_GATES:
        if os.getenv(gate, "").strip() != "1":
            pytest.skip(f"Set {gate}=1 to run the live-execute smoke (SENDS REAL ORDERS)")
    if not EXEC_SMOKE_CONFIG.exists():
        pytest.skip(f"Exec smoke config does not exist: {EXEC_SMOKE_CONFIG}")
    config = load_config(EXEC_SMOKE_CONFIG)
    if not config.safety.allow_live_order:
        pytest.skip("live-execute smoke requires allow_live_order=true in the config")
    if not config.live_execution.enabled:
        pytest.skip("live-execute smoke requires [live_execution] enabled=true")
    return replace(
        config,
        store_path=config.store_path.parent / "live_execute_smoke.sqlite3",
    )


def remove_sqlite_family(path: Path) -> None:
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if target.exists():
            target.unlink()


def seed_pending_entry_state(config: AppConfig) -> StrategyRuntimeState:
    candidate_time = floor_minute(datetime.now(TAIPEI_TZ))
    state = StrategyRuntimeState(
        state=StrategyState.ENTRY_PENDING,
        candidate_direction=Direction.SHORT_TSM_LONG_QFF,
        candidate_idx=0,
        candidate_time=candidate_time,
        candidate_zscore=config.strategy.entry_z + 0.5,
        running_max_equity=config.strategy.initial_capital_twd,
    )
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        store.save_state(0, candidate_time, state, IndicatorEngine(window=config.strategy.zscore_window))
        store.commit()
    finally:
        store.close()
    return state


def seed_pending_exit_state(config: AppConfig) -> None:
    signal_time = floor_minute(datetime.now(TAIPEI_TZ))
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        resume_state = store.load_resume_state()
        assert resume_state is not None
        assert resume_state.strategy.state == StrategyState.OPEN
        state = resume_state.strategy
        state.state = StrategyState.EXIT_PENDING
        state.exit_signal_idx = resume_state.row_index
        state.exit_signal_time = signal_time
        state.exit_signal_zscore = 0.0
        store.save_state(resume_state.row_index, signal_time, state, resume_state.indicator)
        store.commit()
    finally:
        store.close()


def real_readonly_brokers(config: AppConfig):
    return (
        BinanceReadOnlyBroker(config.live.binance_symbol, config.live.fubon_env_path),
        FubonReadOnlyBroker(config.live.fubon_env_path),
    )


def record_flat_account_reconciliation(config: AppConfig, state: StrategyRuntimeState) -> None:
    """Baseline reconciliation before trading. With a flat (ENTRY_PENDING) strategy
    this asserts the broker account is flat, so the acceptance never starts on top
    of an existing position."""
    brokers = real_readonly_brokers(config)
    try:
        report = BrokerReconciler(
            tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
            qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
        ).reconcile(
            strategy_state=state,
            brokers=brokers,
            tsm_symbol=config.live.binance_symbol,
            qff_symbol=config.live.qff_symbol,
        )
    finally:
        for broker in brokers:
            broker.close()
    assert report.status == ReconciliationStatus.MATCHED, (
        "Account is not flat before the acceptance: "
        + str([issue.to_jsonable() if hasattr(issue, "to_jsonable") else str(issue) for issue in report.issues])
    )
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        store.record_reconciliation_report(report)
        store.commit()
    finally:
        store.close()


def assert_brokers_flat(config: AppConfig) -> None:
    brokers = real_readonly_brokers(config)
    try:
        for broker in brokers:
            snapshot = broker.fetch_snapshot()
            open_positions = [p for p in snapshot.positions if abs(p.quantity) > 1e-9]
            assert not open_positions, f"{snapshot.broker.value} still holds a position: {open_positions}"
            assert not snapshot.open_orders, f"{snapshot.broker.value} still has open orders: {snapshot.open_orders}"
    finally:
        for broker in brokers:
            broker.close()


def dryrun_order_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM orders WHERE order_id LIKE 'DRYRUN-%'").fetchone()[0]


def strategy_state_json(connection: sqlite3.Connection) -> str:
    return connection.execute("SELECT state_json FROM strategy_state WHERE id = 1").fetchone()[0]


def print_m6_stage(message: str) -> None:
    print(f"[M6] {message}", flush=True)


def test_real_live_execute_entry_and_exit_returns_flat() -> None:
    config = load_exec_smoke_config()
    print_m6_stage(f"config loaded store_path={config.store_path}")
    remove_sqlite_family(config.store_path)
    print_m6_stage("sqlite store reset")

    state = seed_pending_entry_state(config)
    print_m6_stage(
        f"pending entry state seeded direction={state.candidate_direction.value}"
    )
    record_flat_account_reconciliation(config, state)
    print_m6_stage("pre-entry read-only reconciliation matched")

    # --- real two-leg entry ---
    entry_output = io.StringIO()
    entry_result = LiveExecuteRunner(
        config, reporter=LiveTerminalReporter(entry_output, color=False)
    ).run(resume=True, max_iterations=130)
    print_m6_stage(
        "entry runner finished "
        f"iterations={entry_result.iterations} "
        f"bars_processed={entry_result.bars_processed} "
        f"plans_recorded={entry_result.plans_recorded} "
        f"qff_symbol={entry_result.qff_symbol}"
    )

    assert entry_result.plans_recorded >= 1
    entry_text = entry_output.getvalue()
    assert "live_execution filled" in entry_text
    assert "post_trade_reconciliation matched" in entry_text

    connection = sqlite3.connect(config.store_path)
    try:
        dryrun_orders = dryrun_order_count(connection)
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        fill_count = connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
        assert dryrun_orders == 0, "live-execute must send real, not DRYRUN, orders"
        assert order_count >= 2
        assert fill_count >= 2
        latest_outcome = connection.execute(
            "SELECT status FROM execution_outcomes ORDER BY outcome_id DESC LIMIT 1"
        ).fetchone()
        assert latest_outcome == ("filled",)
        assert '"state": "open"' in strategy_state_json(connection)
    finally:
        connection.close()
    print_m6_stage(
        f"entry verified real_orders={order_count} fills={fill_count} "
        "latest_outcome=filled state=open"
    )

    # --- real two-leg exit ---
    seed_pending_exit_state(config)
    print_m6_stage("pending exit state seeded")
    exit_output = io.StringIO()
    exit_result = LiveExecuteRunner(
        config, reporter=LiveTerminalReporter(exit_output, color=False)
    ).run(resume=True, max_iterations=70)
    print_m6_stage(
        "exit runner finished "
        f"iterations={exit_result.iterations} "
        f"bars_processed={exit_result.bars_processed} "
        f"plans_recorded={exit_result.plans_recorded} "
        f"qff_symbol={exit_result.qff_symbol}"
    )

    exit_text = exit_output.getvalue()
    assert "live_execution filled" in exit_text
    assert "post_trade_reconciliation matched" in exit_text

    connection = sqlite3.connect(config.store_path)
    try:
        dryrun_orders = dryrun_order_count(connection)
        trade_count = connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert dryrun_orders == 0
        assert trade_count == 1
        assert '"state": "flat"' in strategy_state_json(connection)
    finally:
        connection.close()
    print_m6_stage(f"exit verified trades={trade_count} state=flat")

    # --- final safety: the broker account must be flat again ---
    assert_brokers_flat(config)
    print_m6_stage("final broker flat check passed")
