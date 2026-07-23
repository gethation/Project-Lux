from __future__ import annotations

import sqlite3
from datetime import datetime
from functools import partial
from pathlib import Path

import pytest

import lux_trader.cli.commands_live as commands_live
from lux_trader.cli.commands_live import command_margin_check
from lux_trader.cli.parser import build_parser
from lux_trader.config import load_config
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.models import BrokerName, MarketBar, StrategyState
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.margin.monitor import MarginMonitor
from lux_trader.margin.service import MarginCheckService, record_and_report_decision
from lux_trader.reconciliation import FakeReadOnlyBroker
from lux_trader.reconciliation.models import BrokerMarginSnapshot
from lux_trader.store import SQLiteStore

from fakes import write_test_config


USDTTWD = 31.8
NOTIONAL = 1_000_000.0


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


write_config = partial(write_test_config, margin_enabled=True)


def fubon_broker(equity_twd: float, maint_twd: float = 103_500.0) -> FakeReadOnlyBroker:
    return FakeReadOnlyBroker(
        BrokerName.FUBON,
        account_id="FAKE-FUBON",
        margins=(
            BrokerMarginSnapshot(
                broker=BrokerName.FUBON,
                currency="TWD",
                equity=equity_twd,
                raw={"today_equity": equity_twd, "maintenance_margin": maint_twd},
            ),
        ),
    )


def binance_broker(equity_usdt: float, maint_usdt: float = 800.0) -> FakeReadOnlyBroker:
    return FakeReadOnlyBroker(
        BrokerName.BINANCE,
        account_id="FAKE-BINANCE",
        margins=(
            BrokerMarginSnapshot(
                broker=BrokerName.BINANCE,
                currency="USDT",
                equity=equity_usdt,
                raw={
                    "totalMarginBalance": equity_usdt,
                    "totalMaintMargin": maint_usdt,
                },
            ),
        ),
    )


class RecordingReporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def warn(self, timestamp, code, detail="") -> None:
        self.events.append(("WARN", str(code), str(detail)))

    def event(self, timestamp, code, detail="") -> None:
        self.events.append(("EVENT", str(code), str(detail)))

    def error(self, timestamp, message) -> None:
        self.events.append(("ERR", str(message), ""))

    def codes(self) -> list[str]:
        return [code for _, code, _ in self.events]


def open_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "project_lux.sqlite3")
    store.initialize()
    return store


# --- service ----------------------------------------------------------------


def test_service_reads_snapshots_and_records_decision(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    try:
        service = MarginCheckService(
            config,
            brokers=(fubon_broker(300_000.0), binance_broker(300_000.0 / USDTTWD)),
            usdttwd_rate=lambda: USDTTWD,
            clock=lambda: ts("2026-07-06T10:00:05+08:00"),
        )
        decision = service.run_check(check_type="daily", position_open=True)
        record_and_report_decision(decision, store=store, reporter=reporter)
        store.commit()

        assert decision.level == "ok"
        assert decision.binance.ratio == pytest.approx(0.30)
        assert decision.fubon.ratio == pytest.approx(0.30)
        # maintenance margin flows from broker raw payloads
        assert decision.fubon.maint_margin_twd == pytest.approx(103_500.0)
        assert decision.binance.maint_margin_twd == pytest.approx(800.0 * USDTTWD)
        assert reporter.codes() == ["margin_check"]

        row = store.load_last_margin_check()
        assert row is not None
        assert row["check_type"] == "daily"
        assert row["level"] == "ok"
        assert row["fubon_ratio"] == pytest.approx(0.30)
    finally:
        store.close()


def test_service_transfer_decision_reports_warn(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    try:
        service = MarginCheckService(
            config,
            brokers=(fubon_broker(500_000.0), binance_broker(usd_ratio(0.10))),
            usdttwd_rate=lambda: USDTTWD,
        )
        decision = service.run_check(check_type="daily", position_open=True)
        record_and_report_decision(decision, store=store, reporter=reporter)
        store.commit()

        assert decision.level == "transfer"
        assert reporter.codes() == ["margin_transfer_required"]
        row = store.load_last_margin_check()
        assert row["transfer_direction"] == "fubon->binance"
        assert row["transfer_amount_twd"] == pytest.approx(200_000.0)
    finally:
        store.close()


def usd_ratio(ratio: float) -> float:
    return ratio * NOTIONAL / USDTTWD


# --- monitor scheduling -----------------------------------------------------


def flat_state() -> StrategyRuntimeState:
    return StrategyRuntimeState(state=StrategyState.FLAT)


def open_state() -> StrategyRuntimeState:
    return StrategyRuntimeState(state=StrategyState.OPEN)


def make_monitor(config, *, fubon=None, binance=None) -> MarginMonitor:
    return MarginMonitor(
        config,
        usdttwd_rate=lambda: USDTTWD,
        brokers_factory=lambda: (
            fubon or fubon_broker(300_000.0),
            binance or binance_broker(usd_ratio(0.30)),
        ),
    )


def test_monitor_daily_check_fires_once_after_check_time(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    monitor = make_monitor(config)
    try:
        # Monday 2026-07-06: before 10:00 nothing fires
        monitor.maybe_run(
            ts("2026-07-06T09:59:59+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == []

        monitor.maybe_run(
            ts("2026-07-06T10:00:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        monitor.maybe_run(
            ts("2026-07-06T10:00:02+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == ["margin_check"]

        # next day fires again
        monitor.maybe_run(
            ts("2026-07-07T10:00:00+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == ["margin_check", "margin_check"]
    finally:
        monitor.close()
        store.close()


def test_monitor_daily_guard_survives_restart_via_store(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    try:
        first = make_monitor(config)
        first.maybe_run(
            ts("2026-07-06T10:00:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        first.close()
        assert reporter.codes() == ["margin_check"]

        # a fresh monitor (restart) must not re-fire the same day
        second = make_monitor(config)
        second.maybe_run(
            ts("2026-07-06T11:00:00+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        second.close()
        assert reporter.codes() == ["margin_check"]
    finally:
        store.close()


def test_monitor_margin_notional_prefers_fixed_tw_leg_lots(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(
        write_config(
            tmp_path,
            tw_leg_lots=1,
            margin_leg_notional_twd=1_000_000.0,
            tw_leg_symbol="auto",
        )
    )
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    monitor = make_monitor(
        config,
        fubon=fubon_broker(75_000.0),
        binance=binance_broker(75_000.0 / USDTTWD),
    )
    try:
        store.record_warmup_bars(
            [
                MarketBar(
                    row_index=0,
                    timestamp=ts("2026-07-06T09:59:00+08:00"),
                    tw_leg_close=2500.0,
                    tw_leg_close_filled=2500.0,
                    us_leg_twd_fair=3000.0,
                    spread=0.0,
                    tw_leg_symbol="QFFG6",
                )
            ]
        )
        monitor.maybe_run(
            ts("2026-07-06T10:00:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )

        assert reporter.codes() == ["margin_check"]
        row = store.load_last_margin_check("daily")
        assert row is not None
        assert row["binance_ratio"] == pytest.approx(0.30)
        assert row["fubon_ratio"] == pytest.approx(0.30)
    finally:
        store.close()


def test_monitor_skips_weekends(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    monitor = make_monitor(config)
    try:
        # Saturday 2026-07-11
        monitor.maybe_run(
            ts("2026-07-11T10:00:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == []
    finally:
        monitor.close()
        store.close()


def test_monitor_red_line_cadence_while_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    monitor = make_monitor(
        config,
        binance=binance_broker(usd_ratio(0.04), maint_usdt=usd_ratio(0.035)),
    )
    try:
        # 11:00 (daily already due; run it first so red-line cadence is clean)
        monitor.maybe_run(
            ts("2026-07-06T11:00:00+08:00"),
            strategy_state=open_state(),
            store=store,
            reporter=reporter,
        )
        # daily check at 4% while open is already red line
        assert "margin_red_line" in reporter.codes()
        before = len(reporter.events)

        # within 15 minutes: no new check
        monitor.maybe_run(
            ts("2026-07-06T11:10:00+08:00"),
            strategy_state=open_state(),
            store=store,
            reporter=reporter,
        )
        assert len(reporter.events) == before

        # after 15 minutes: red-line check fires
        monitor.maybe_run(
            ts("2026-07-06T11:15:00+08:00"),
            strategy_state=open_state(),
            store=store,
            reporter=reporter,
        )
        assert len(reporter.events) > before
        row = store.load_last_margin_check("red_line")
        assert row is not None
        assert row["level"] == "red_line"
    finally:
        monitor.close()
        store.close()


def test_monitor_requires_env_gate_and_warns_once(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LUX_READONLY_BROKER", raising=False)
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    monitor = make_monitor(config)
    try:
        for stamp in ("10:00:01", "10:00:02", "10:00:03"):
            monitor.maybe_run(
                ts(f"2026-07-06T{stamp}+08:00"),
                strategy_state=flat_state(),
                store=store,
                reporter=reporter,
            )
        assert reporter.codes() == ["margin_check_disabled"]
        assert store.load_last_margin_check() is None
    finally:
        monitor.close()
        store.close()


def test_monitor_broker_failure_warns_and_retries_after_backoff(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(write_config(tmp_path))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    failing = FakeReadOnlyBroker(
        BrokerName.FUBON, fetch_error=RuntimeError("fubon down")
    )
    monitor = MarginMonitor(
        config,
        usdttwd_rate=lambda: USDTTWD,
        brokers_factory=lambda: (failing, binance_broker(usd_ratio(0.30))),
    )
    try:
        monitor.maybe_run(
            ts("2026-07-06T10:00:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == ["margin_check_failed"]

        # inside backoff: silent
        monitor.maybe_run(
            ts("2026-07-06T10:05:00+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == ["margin_check_failed"]

        # after backoff: retried (fails again, still no daily-done marker)
        monitor.maybe_run(
            ts("2026-07-06T10:15:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.codes() == ["margin_check_failed", "margin_check_failed"]
        assert store.load_last_margin_check() is None
    finally:
        monitor.close()
        store.close()


def test_monitor_disabled_config_is_inert(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config = load_config(write_config(tmp_path, margin_enabled=False))
    store = open_store(tmp_path)
    reporter = RecordingReporter()
    monitor = make_monitor(config)
    try:
        monitor.maybe_run(
            ts("2026-07-06T10:00:01+08:00"),
            strategy_state=flat_state(),
            store=store,
            reporter=reporter,
        )
        assert reporter.events == []
    finally:
        monitor.close()
        store.close()


# --- CLI ---------------------------------------------------------------------


def test_margin_check_cli_prints_guidance_and_records(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config_path = write_config(tmp_path)
    monkeypatch.setattr(
        commands_live,
        "build_margin_brokers",
        lambda config: (fubon_broker(500_000.0), binance_broker(usd_ratio(0.10))),
    )
    monkeypatch.setattr(commands_live, "fetch_usdttwd_rate", lambda config: USDTTWD)

    args = build_parser().parse_args(["status", "margin", "--config", str(config_path)])
    exit_code = command_margin_check(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "level=transfer" in output
    assert "fubon->binance" in output
    assert "10:00" in output

    connection = sqlite3.connect(tmp_path / "project_lux.sqlite3")
    try:
        count = connection.execute("SELECT COUNT(*) FROM margin_checks").fetchone()[0]
        assert count == 1
    finally:
        connection.close()


def test_margin_check_cli_red_line_exits_nonzero(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    config_path = write_config(tmp_path)
    # seed an OPEN position so the red line applies
    config = load_config(config_path)
    store = SQLiteStore(config.store_path, **config.store_identity())
    try:
        store.initialize()
        store.save_state(
            0,
            ts("2026-07-06T09:00:00+08:00"),
            StrategyRuntimeState(state=StrategyState.OPEN),
            IndicatorEngine(window=500),
        )
        store.commit()
    finally:
        store.close()

    monkeypatch.setattr(
        commands_live,
        "build_margin_brokers",
        lambda config: (fubon_broker(500_000.0), binance_broker(usd_ratio(0.04))),
    )
    monkeypatch.setattr(commands_live, "fetch_usdttwd_rate", lambda config: USDTTWD)

    args = build_parser().parse_args(["status", "margin", "--config", str(config_path)])
    exit_code = command_margin_check(args)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "level=red_line" in output
    assert "立即平倉" in output


def test_margin_check_cli_requires_env_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LUX_READONLY_BROKER", raising=False)
    args = build_parser().parse_args(
        ["status", "margin", "--config", str(write_config(tmp_path))]
    )
    with pytest.raises(SystemExit, match="LUX_READONLY_BROKER=1"):
        command_margin_check(args)


def test_margin_check_cli_requires_enabled_config(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    args = build_parser().parse_args(
        ["status", "margin", "--config", str(write_config(tmp_path, margin_enabled=False))]
    )
    with pytest.raises(SystemExit, match="enabled=true"):
        command_margin_check(args)
