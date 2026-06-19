from __future__ import annotations

import io
import os
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from lux_trader.config import AppConfig, load_config
from lux_trader.indicator import IndicatorEngine
from lux_trader.live_market_data import TAIPEI_TZ, floor_minute
from lux_trader.live_runner import LiveDryRunRunner
from lux_trader.models import Direction, StrategyState
from lux_trader.readonly_brokers import BinanceReadOnlyBroker, FubonReadOnlyBroker
from lux_trader.reconciliation import BrokerReconciler, ReconciliationStatus
from lux_trader.store import SQLiteStore
from lux_trader.strategy import StrategyRuntimeState
from lux_trader.terminal_ui import LiveTerminalReporter


SMOKE_CONFIG = Path("config.live.smoke.local.toml")

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
        store_path=config.store_path.parent / "live_dry_run_readonly_smoke.sqlite3",
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
        candidate_direction=Direction.SHORT_TSM_LONG_QFF,
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


def test_real_readonly_reconciliation_then_live_dry_run_records_intent() -> None:
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
    assert "dry_run_intent" in output

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
        assert counts["execution_legs"] >= 2
        assert counts["orders"] == 0
        assert counts["fills"] == 0
        assert counts["trades"] == 0

        source_counts = {
            source: count
            for source, count in connection.execute(
                "SELECT source, COUNT(*) FROM market_ticks GROUP BY source"
            ).fetchall()
        }
        assert {"fubon_qff", "binanceusdm", "bitopro"}.issubset(source_counts)

        latest_plan = connection.execute(
            """
            SELECT status, plan_type
            FROM execution_plans
            ORDER BY timestamp DESC, row_index DESC, plan_id DESC
            LIMIT 1
            """
        ).fetchone()
        assert latest_plan == ("recorded", "entry")
    finally:
        connection.close()
