from __future__ import annotations

import sqlite3
from pathlib import Path

from lux_trader.cli import (
    build_parser,
    command_broker_doctor,
    command_reconcile_brokers,
)
from lux_trader.models import BrokerName
from lux_trader.reconciliation import (
    BrokerReconciler,
    FakeReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.store import SQLiteStore


def write_config(tmp_path: Path) -> Path:
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
                "[live_market_data]",
                "qff_symbol = 'QFFG6'",
                "binance_symbol = 'TSM/USDT:USDT'",
                f"taifex_cache_dir = '{cache_dir}'",
                "",
                "[broker_reconciliation]",
                "enabled = false",
                "fail_on_mismatch = false",
                "tsm_units_tolerance = 0.000001",
                "qff_contract_tolerance = 0",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def count_table(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_store_initializes_reconciliation_tables(tmp_path: Path) -> None:
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

    assert "broker_reconciliation_runs" in tables
    assert "broker_snapshots" in tables
    assert "broker_reconciliation_issues" in tables


def test_store_records_and_loads_latest_reconciliation_report(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "project_lux.sqlite3")
    try:
        store.initialize()
        report = BrokerReconciler().reconcile(
            strategy_state=None,
            brokers=(
                FakeReadOnlyBroker(BrokerName.BINANCE_TSM),
                FakeReadOnlyBroker(BrokerName.FUBON_QFF),
            ),
            tsm_symbol="TSM/USDT:USDT",
            qff_symbol="QFFG6",
        )
        run_id = store.record_reconciliation_report(report)
        store.commit()

        loaded = store.load_latest_reconciliation_report()
        assert loaded is not None
        assert run_id == 1
        assert loaded.status == ReconciliationStatus.MATCHED
        assert loaded.expected.qff_symbol == "QFFG6"
        assert count_table(store.connection, "broker_reconciliation_runs") == 1
        assert count_table(store.connection, "broker_snapshots") == 2
        assert count_table(store.connection, "broker_reconciliation_issues") == 0
    finally:
        store.close()


def test_broker_doctor_cli_succeeds_without_private_api(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    args = parser.parse_args(["broker-doctor", "--config", str(write_config(tmp_path))])

    exit_code = command_broker_doctor(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Broker doctor checks passed" in output
    assert "private_api=disabled" in output


def test_reconcile_brokers_fake_matched_records_report(
    tmp_path: Path,
    capsys,
) -> None:
    config_path = write_config(tmp_path)
    parser = build_parser()
    args = parser.parse_args(["reconcile-brokers", "--config", str(config_path), "--fake"])

    exit_code = command_reconcile_brokers(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status=matched" in output

    connection = sqlite3.connect(tmp_path / "project_lux.sqlite3")
    try:
        assert count_table(connection, "broker_reconciliation_runs") == 1
    finally:
        connection.close()


def test_reconcile_brokers_fake_mismatch_warns_but_exits_zero(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "reconcile-brokers",
            "--config",
            str(write_config(tmp_path)),
            "--fake",
            "--fake-case",
            "mismatch",
        ]
    )

    exit_code = command_reconcile_brokers(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status=warning" in output
    assert "unexpected_position" in output


def test_reconcile_brokers_fake_error_exits_nonzero(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "reconcile-brokers",
            "--config",
            str(write_config(tmp_path)),
            "--fake",
            "--fake-case",
            "error",
        ]
    )

    exit_code = command_reconcile_brokers(args)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "status=error" in output
    assert "broker_fetch_failed" in output


def test_reconcile_brokers_readonly_requires_env_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LUX_READONLY_BROKER", raising=False)
    parser = build_parser()
    args = parser.parse_args(
        [
            "reconcile-brokers",
            "--config",
            str(write_config(tmp_path)),
            "--readonly",
        ]
    )

    try:
        command_reconcile_brokers(args)
    except SystemExit as exc:
        assert "LUX_READONLY_BROKER=1" in str(exc)
    else:
        raise AssertionError("Expected SystemExit")
