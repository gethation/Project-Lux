from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

import lux_trader.cli.commands_execution as commands_execution
import lux_trader.cli.commands_live as commands_live
from fakes import make_fake_broker_builder, write_execution_test_config
from lux_trader.cli.commands_execution import command_live_execute
from lux_trader.cli.parser import build_parser
from lux_trader.reconciliation import ReconciliationStatus
from lux_trader.store import SQLiteStore


write_live_execute_config = partial(
    write_execution_test_config,
    config_name="config.live-execute.toml",
    store_name="live-execute.sqlite3",
    cache_name="taifex_cache",
    include_broker_reconciliation=True,
    fubon_env_path=None,
)


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


def inject_fake_shared_brokers(monkeypatch, fake_case: str) -> None:
    builder = make_fake_broker_builder(fake_case)

    def shared(config, _tw_leg_symbol):
        brokers = builder(config, None, readonly=True)
        return brokers[0], brokers

    monkeypatch.setattr(
        commands_execution,
        "build_live_execution_brokers",
        shared,
    )


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
    inject_fake_shared_brokers(monkeypatch, "matched")
    run_calls: list[dict[str, object]] = []

    class FakeLiveExecuteRunner:
        def __init__(self, config, *, reporter, **_kwargs):  # noqa: ARG002
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
            "live", "--mode", "execute",
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
    inject_fake_shared_brokers(monkeypatch, "mismatch")

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
            "live", "--mode", "execute",
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
