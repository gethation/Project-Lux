from __future__ import annotations

from pathlib import Path

import pytest

import lux_trader.cli.commands_execution as commands_execution
import lux_trader.cli.commands_live as commands_live
from fakes import make_fake_broker_builder
from lux_trader.cli.commands_execution import command_live_execute
from lux_trader.cli.parser import build_parser
from lux_trader.reconciliation import ReconciliationStatus
from lux_trader.store import SQLiteStore


def write_live_execute_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.live-execute.toml"
    store_path = (tmp_path / "live-execute.sqlite3").as_posix()
    cache_dir = (tmp_path / "taifex_cache").as_posix()
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                "input_csv = ''",
                f"store_path = '{store_path}'",
                "",
                "[safety]",
                "allow_live_order = true",
                "",
                "[live_market_data]",
                "qff_symbol = 'QFFG6'",
                "binance_symbol = 'TSM/USDT:USDT'",
                "bitopro_symbol = 'USDT/TWD'",
                f"taifex_cache_dir = '{cache_dir}'",
                "",
                "[broker_reconciliation]",
                "enabled = true",
                "fail_on_mismatch = true",
                "tsm_units_tolerance = 0.000001",
                "qff_contract_tolerance = 0",
                "",
                "[live_execution]",
                "enabled = true",
                "require_readonly_reconciliation = true",
                "max_plan_age_seconds = 120",
                "qff_first = true",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def set_live_order_env(monkeypatch) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("BINANCE_ALLOW_LIVE_ORDER", "1")


def latest_reconciliation(store_path: Path):
    store = SQLiteStore(store_path)
    try:
        store.initialize()
        return store.load_latest_reconciliation_report()
    finally:
        store.close()


def test_live_execute_refreshes_matched_reconciliation_before_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = write_live_execute_config(tmp_path)
    store_path = tmp_path / "live-execute.sqlite3"
    set_live_order_env(monkeypatch)
    monkeypatch.setattr(
        commands_live,
        "build_reconciliation_brokers",
        make_fake_broker_builder("matched"),
    )
    run_calls: list[dict[str, object]] = []

    class FakeLiveExecuteRunner:
        def __init__(self, config, *, reporter):  # noqa: ARG002
            pass

        def run(self, **kwargs):
            report = latest_reconciliation(store_path)
            assert report is not None
            assert report.status == ReconciliationStatus.MATCHED
            run_calls.append(kwargs)

    monkeypatch.setattr(
        commands_execution,
        "LiveExecuteRunner",
        FakeLiveExecuteRunner,
    )
    args = build_parser().parse_args(
        [
            "live-execute",
            "--config",
            str(config_path),
            "--reset-store",
            "--max-iterations",
            "1",
            "--quiet-ui",
        ]
    )

    assert command_live_execute(args) == 0
    assert len(run_calls) == 1
    assert run_calls[0]["reset_store"] is False


def test_live_execute_stops_before_runner_when_reconciliation_mismatches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = write_live_execute_config(tmp_path)
    store_path = tmp_path / "live-execute.sqlite3"
    set_live_order_env(monkeypatch)
    monkeypatch.setattr(
        commands_live,
        "build_reconciliation_brokers",
        make_fake_broker_builder("mismatch"),
    )

    class RunnerMustNotStart:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("live runner started after reconciliation mismatch")

    monkeypatch.setattr(
        commands_execution,
        "LiveExecuteRunner",
        RunnerMustNotStart,
    )
    args = build_parser().parse_args(
        [
            "live-execute",
            "--config",
            str(config_path),
            "--reset-store",
            "--quiet-ui",
        ]
    )

    with pytest.raises(SystemExit, match="readonly_reconciliation_matched"):
        command_live_execute(args)

    report = latest_reconciliation(store_path)
    assert report is not None
    assert report.status == ReconciliationStatus.WARNING
