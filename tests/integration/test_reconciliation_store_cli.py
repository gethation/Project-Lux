from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import lux_trader.cli.commands_live as commands_live
from lux_trader.cli.commands_live import command_reconcile_brokers
from lux_trader.cli.parser import build_parser
from lux_trader.core.models import BrokerName
from lux_trader.reconciliation import (
    BrokerReconciler,
    FakeReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.store import SQLiteStore

from fakes import make_fake_broker_builder, write_test_config


write_config = partial(write_test_config, include_broker_reconciliation=True)


def count_table(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def use_fake_brokers(monkeypatch, fake_case: str) -> None:
    monkeypatch.setattr(
        commands_live,
        "build_reconciliation_brokers",
        make_fake_broker_builder(fake_case),
    )


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


def test_reconcile_brokers_fake_matched_records_report(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    config_path = write_config(tmp_path)
    use_fake_brokers(monkeypatch, "matched")
    args = build_parser().parse_args(["reconcile-brokers", "--config", str(config_path)])

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
    monkeypatch,
) -> None:
    use_fake_brokers(monkeypatch, "mismatch")
    args = build_parser().parse_args(
        ["reconcile-brokers", "--config", str(write_config(tmp_path))]
    )

    exit_code = command_reconcile_brokers(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "status=warning" in output
    assert "unexpected_position" in output


def test_reconcile_brokers_fake_error_exits_nonzero(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    use_fake_brokers(monkeypatch, "error")
    args = build_parser().parse_args(
        ["reconcile-brokers", "--config", str(write_config(tmp_path))]
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
    args = build_parser().parse_args(
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


def test_reconcile_brokers_without_readonly_refuses_real_brokers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("LUX_READONLY_BROKER", raising=False)
    args = build_parser().parse_args(
        ["reconcile-brokers", "--config", str(write_config(tmp_path))]
    )

    try:
        command_reconcile_brokers(args)
    except SystemExit as exc:
        assert "--readonly" in str(exc)
    else:
        raise AssertionError("Expected SystemExit without --readonly")


def test_reconcile_brokers_allows_live_order_config_for_readonly_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = write_config(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n[safety]\nallow_live_order = true\n",
        encoding="utf-8",
    )
    use_fake_brokers(monkeypatch, "matched")
    args = build_parser().parse_args(
        [
            "reconcile-brokers",
            "--config",
            str(config_path),
            "--readonly",
        ]
    )

    assert command_reconcile_brokers(args) == 0
