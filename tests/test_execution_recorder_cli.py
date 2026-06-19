from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime

from lux_trader.cli import (
    build_parser,
    command_dry_run_doctor,
    command_execution_summary,
    command_live_dry_run,
)
from lux_trader.execution_intent import ExecutionLeg, ExecutionPlanType, PairExecutionPlan
from lux_trader.execution_recorder import DryRunExecutionRecorder
from lux_trader.models import BrokerName, Direction, OrderSide
from lux_trader.store import SQLiteStore


def write_config(tmp_path: Path, *, allow_live_order: bool = False) -> Path:
    config_path = tmp_path / "config.test.toml"
    store_path = (tmp_path / "project_lux.sqlite3").as_posix()
    cache_dir = (tmp_path / "taifex_cache").as_posix()
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                "input_csv = ''",
                f"store_path = '{store_path}'",
                "",
                "[safety]",
                f"allow_live_order = {str(allow_live_order).lower()}",
                "validate_expected_zscore = false",
                "expected_zscore_tolerance = 0.0000001",
                "",
                "[live_market_data]",
                "qff_symbol = 'QFFG6'",
                "binance_symbol = 'TSM/USDT:USDT'",
                f"taifex_cache_dir = '{cache_dir}'",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def count_table(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def short_entry_plan() -> PairExecutionPlan:
    timestamp = datetime.fromisoformat("2026-02-02T09:15:00+08:00")
    return PairExecutionPlan(
        plan_id="EXEC-TEST",
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_TSM_LONG_QFF,
        timestamp=timestamp,
        row_index=88,
        legs=(
            ExecutionLeg(
                broker=BrokerName.BINANCE_TSM,
                symbol="TSM/USDT:USDT",
                side=OrderSide.SELL,
                quantity=125.5,
                price=720.0,
                timestamp=timestamp,
                row_index=88,
                qff_symbol="QFFG6",
                qff_expiry="2026-02-18",
                contract_policy_state="active",
            ),
            ExecutionLeg(
                broker=BrokerName.FUBON_QFF,
                symbol="QFFG6",
                side=OrderSide.BUY,
                quantity=3,
                price=1180.0,
                timestamp=timestamp,
                row_index=88,
                qff_symbol="QFFG6",
                qff_expiry="2026-02-18",
                contract_policy_state="active",
            ),
        ),
        reason="entry_zscore_crossed",
        decision_zscore=2.14,
        decision_spread_type="shortSpread",
        qff_symbol="QFFG6",
        qff_expiry="2026-02-18",
        contract_policy_state="active",
    )


def test_store_initializes_execution_tables(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "project_lux.sqlite3")
    try:
        store.initialize()
        tables = {
            row["name"]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        store.close()

    assert "execution_plans" in tables
    assert "execution_legs" in tables
    assert "execution_checks" in tables


def test_recorder_records_valid_plan_without_orders_fills_or_trades(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "project_lux.sqlite3")
    try:
        store.initialize()
        plan = DryRunExecutionRecorder(store).record_plan(short_entry_plan())
        store.commit()

        assert plan.status.value == "recorded"
        assert count_table(store.connection, "execution_plans") == 1
        assert count_table(store.connection, "execution_legs") == 2
        assert count_table(store.connection, "execution_checks") > 0
        assert count_table(store.connection, "orders") == 0
        assert count_table(store.connection, "fills") == 0
        assert count_table(store.connection, "trades") == 0

        latest = store.load_latest_execution_plan_payload()
        summary = store.build_execution_summary()
        assert latest is not None
        assert latest["status"] == "recorded"
        assert summary["plan_count"] == 1
        assert summary["status_counts"] == {"recorded": 1}
        assert summary["orders"] == 0
        assert summary["fills"] == 0
        assert summary["trades"] == 0
    finally:
        store.close()


def test_dry_run_doctor_cli_initializes_store(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    args = parser.parse_args(["dry-run-doctor", "--config", str(write_config(tmp_path))])

    exit_code = command_dry_run_doctor(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Dry-run doctor checks passed" in output
    assert "private_api=disabled" in output


def test_live_dry_run_fake_records_plan_and_exits_zero(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "live-dry-run",
            "--config",
            str(write_config(tmp_path)),
            "--fake",
            "--reset-store",
        ]
    )

    exit_code = command_live_dry_run(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Live dry-run skeleton complete" in output
    assert "status=recorded" in output

    connection = sqlite3.connect(tmp_path / "project_lux.sqlite3")
    try:
        assert count_table(connection, "execution_plans") == 1
        assert count_table(connection, "execution_legs") == 2
        assert count_table(connection, "orders") == 0
        assert count_table(connection, "fills") == 0
        assert count_table(connection, "trades") == 0
    finally:
        connection.close()


def test_live_dry_run_fake_rejected_records_plan_and_exits_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "live-dry-run",
            "--config",
            str(write_config(tmp_path)),
            "--fake",
            "--fake-case",
            "rejected",
            "--reset-store",
        ]
    )

    exit_code = command_live_dry_run(args)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "status=rejected" in output
    assert "failed_checks=1" in output


def test_execution_summary_cli_prints_json(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    config_path = write_config(tmp_path)
    command_live_dry_run(
        parser.parse_args(
            [
                "live-dry-run",
                "--config",
                str(config_path),
                "--fake",
                "--reset-store",
            ]
        )
    )
    capsys.readouterr()

    exit_code = command_execution_summary(
        parser.parse_args(["execution-summary", "--config", str(config_path)])
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert exit_code == 0
    assert payload["plan_count"] == 1
    assert payload["status_counts"] == {"recorded": 1}


def test_live_dry_run_rejects_allow_live_order(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "live-dry-run",
            "--config",
            str(write_config(tmp_path, allow_live_order=True)),
            "--fake",
        ]
    )

    try:
        command_live_dry_run(args)
    except SystemExit as exc:
        assert "allow_live_order" in str(exc)
    else:
        raise AssertionError("Expected SystemExit")
