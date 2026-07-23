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
from lux_trader.core.time import TAIPEI_TZ
from lux_trader.market_data import floor_minute
from lux_trader.runtime.live import LiveDryRunRunner
from lux_trader.core.models import Direction, StrategyState
from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.reconciliation import BrokerReconciler, ReconciliationStatus
from lux_trader.store import SQLiteStore
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.terminal_ui import LiveTerminalReporter


SMOKE_CONFIG = Path("configs/config.live.smoke.local.toml")

pytestmark = [
    pytest.mark.live_marketdata,
    pytest.mark.readonly_broker,
    pytest.mark.dry_run_smoke,
]


def load_integrated_smoke_config() -> AppConfig:
    if os.getenv("LUX_LIVE_MARKETDATA", "").strip() != "1":
        pytest.skip("Set LUX_LIVE_MARKETDATA=1 to run dry-run smoke tests")
    if os.getenv("LUX_READONLY_BROKER", "").strip() != "1":
        pytest.skip("Set LUX_READONLY_BROKER=1 to run dry-run smoke tests")
    if not SMOKE_CONFIG.exists():
        pytest.skip(f"Smoke config does not exist: {SMOKE_CONFIG}")
    config = load_config(SMOKE_CONFIG)
    if config.safety.allow_live_order:
        pytest.fail("Dry-run smoke requires allow_live_order=false")
    return replace(
        config,
        store_path=config.store_path.parent / "live_dry_run_full_smoke.sqlite3",
    )


def remove_sqlite_family(path: Path) -> None:
    for target in (
        path,
        path.with_name(path.name + "-wal"),
        path.with_name(path.name + "-shm"),
    ):
        if target.exists():
            target.unlink()


def seed_pending_entry_state(config: AppConfig) -> StrategyRuntimeState:
    candidate_time = floor_minute(datetime.now(TAIPEI_TZ))
    state = StrategyRuntimeState(
        state=StrategyState.ENTRY_PENDING,
        candidate_direction=Direction.SHORT_US_LONG_TW,
        candidate_idx=0,
        candidate_time=candidate_time,
        candidate_zscore=config.strategy.entry_z + 0.5,
        running_max_equity=config.strategy.initial_capital_twd,
    )
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        store.save_state(
            0,
            candidate_time,
            state,
            IndicatorEngine(window=config.strategy.zscore_window),
        )
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
        store.save_state(
            resume_state.row_index,
            signal_time,
            state,
            resume_state.indicator,
        )
        store.commit()
    finally:
        store.close()


def record_real_readonly_reconciliation(
    config: AppConfig,
    state: StrategyRuntimeState,
) -> None:
    brokers = (
        BinanceReadOnlyBroker(config.live.binance_symbol, config.live.fubon_env_path),
        FubonReadOnlyBroker(config.live.fubon_env_path),
    )
    try:
        report = BrokerReconciler(
            us_leg_units_tolerance=config.broker_reconciliation.us_leg_units_tolerance,
            tw_leg_contract_tolerance=config.broker_reconciliation.tw_leg_contract_tolerance,
        ).reconcile(
            strategy_state=state,
            brokers=brokers,
            us_leg_symbol=config.live.binance_symbol,
            tw_leg_symbol=config.live.tw_leg_symbol,
        )
    finally:
        for broker in brokers:
            broker.close()

    assert report.status == ReconciliationStatus.MATCHED, [
        issue.to_jsonable() if hasattr(issue, "to_jsonable") else str(issue)
        for issue in report.issues
    ]

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        store.record_reconciliation_report(report)
        store.commit()
    finally:
        store.close()


def test_real_live_dry_run_simulates_entry_exit_and_resume() -> None:
    config = load_integrated_smoke_config()
    remove_sqlite_family(config.store_path)
    state = seed_pending_entry_state(config)
    record_real_readonly_reconciliation(config, state)

    terminal_output = io.StringIO()
    result = LiveDryRunRunner(
        config,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(resume=True, max_iterations=130)

    assert result.iterations == 130
    assert result.plans_recorded >= 1
    output = terminal_output.getvalue()
    assert "EVENT startup live_loop" in output
    assert "execution_filled" in output

    connection = sqlite3.connect(config.store_path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "broker_reconciliation_runs",
                "warmup_bars",
                "market_ticks",
                "live_runs",
                "execution_plans",
                "execution_outcomes",
                "execution_legs",
                "orders",
                "fills",
                "trades",
            )
        }
        assert counts["broker_reconciliation_runs"] == 1
        assert counts["warmup_bars"] == config.live.warmup_minutes
        assert counts["market_ticks"] > 0
        assert counts["live_runs"] == 1
        assert counts["execution_plans"] >= 1
        assert counts["execution_outcomes"] >= 1
        assert counts["execution_legs"] >= 2
        assert counts["orders"] >= 2
        assert counts["fills"] >= 2
        assert counts["trades"] == 0

        source_counts = {
            source: count
            for source, count in connection.execute(
                "SELECT source, COUNT(*) FROM market_ticks GROUP BY source"
            ).fetchall()
        }
        assert {"fubon_tw_leg", "binanceusdm", "bitopro"}.issubset(source_counts)

        latest_plan = connection.execute(
            """
            SELECT status, plan_type
            FROM execution_plans
            ORDER BY timestamp DESC, row_index DESC, plan_id DESC
            LIMIT 1
            """
        ).fetchone()
        assert latest_plan == ("recorded", "entry")
        latest_outcome = connection.execute(
            """
            SELECT status
            FROM execution_outcomes
            ORDER BY outcome_id DESC
            LIMIT 1
            """
        ).fetchone()
        assert latest_outcome == ("filled",)
    finally:
        connection.close()

    seed_pending_exit_state(config)
    exit_output = io.StringIO()
    exit_result = LiveDryRunRunner(
        config,
        reporter=LiveTerminalReporter(exit_output, color=False),
    ).run(resume=True, max_iterations=70)

    assert exit_result.iterations == 70
    assert exit_result.plans_recorded == 1
    assert "execution_filled" in exit_output.getvalue()

    connection = sqlite3.connect(config.store_path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "warmup_bars",
                "live_runs",
                "execution_plans",
                "execution_outcomes",
                "execution_legs",
                "orders",
                "fills",
                "trades",
            )
        }
        assert counts["warmup_bars"] == config.live.warmup_minutes
        assert counts["live_runs"] == 2
        assert counts["execution_plans"] == 2
        assert counts["execution_outcomes"] == 2
        assert counts["execution_legs"] == 4
        assert counts["orders"] == 4
        assert counts["fills"] == 4
        assert counts["trades"] == 1

        latest_plan = connection.execute(
            """
            SELECT status, plan_type
            FROM execution_plans
            ORDER BY timestamp DESC, row_index DESC, plan_id DESC
            LIMIT 1
            """
        ).fetchone()
        assert latest_plan == ("recorded", "exit")

        state_json = connection.execute(
            "SELECT state_json FROM strategy_state WHERE pair_id = 'qff_tsm'"
        ).fetchone()[0]
        assert '"state": "flat"' in state_json
    finally:
        connection.close()

    resume_output = io.StringIO()
    resume_result = LiveDryRunRunner(
        config,
        reporter=LiveTerminalReporter(resume_output, color=False),
    ).run(resume=True, max_iterations=70)

    assert resume_result.iterations == 70
    assert resume_result.plans_recorded == 0
    assert "warmup_auto" not in resume_output.getvalue()

    connection = sqlite3.connect(config.store_path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "warmup_bars",
                "live_runs",
                "execution_plans",
                "execution_outcomes",
                "orders",
                "fills",
                "trades",
            )
        }
        assert counts["warmup_bars"] == config.live.warmup_minutes
        assert counts["live_runs"] == 3
        assert counts["execution_plans"] == 2
        assert counts["execution_outcomes"] == 2
        assert counts["orders"] == 4
        assert counts["fills"] == 4
        assert counts["trades"] == 1

        duplicate_bars = connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT timestamp
                FROM bars
                GROUP BY timestamp
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        duplicate_plans = connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT timestamp, plan_type, direction
                FROM execution_plans
                GROUP BY timestamp, plan_type, direction
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert duplicate_bars == 0
        assert duplicate_plans == 0
    finally:
        connection.close()
