from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path

import lux_trader.cli.commands_live as commands_live
import lux_trader.cli.commands_recovery as commands_recovery
from lux_trader.cli.commands_live import command_clear_pause, command_live_status
from lux_trader.cli.commands_recovery import command_recover_manual_flat
from lux_trader.cli.parser import build_parser
from lux_trader.config import load_config
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.models import (
    BrokerName,
    Direction,
    Fill,
    OrderRequest,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.store import SQLiteStore

from fakes import make_fake_broker_builder, write_test_config


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


write_config = partial(
    write_test_config,
    allow_live_order=False,
    include_broker_reconciliation=True,
)


def seed_state(
    config_path: Path,
    *,
    state: StrategyState,
    with_position: bool,
) -> None:
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        runtime = StrategyRuntimeState(state=state)
        if with_position:
            runtime.position_direction = Direction.SHORT_TSM_LONG_QFF
            runtime.tsm_units = -100.0
            runtime.qff_contracts = 2
            runtime.trading_qff_symbol = "QFFG6"
        store.save_state(0, ts(), runtime, IndicatorEngine(window=500))
        store.commit()
    finally:
        store.close()


def load_persisted_state(config_path: Path) -> StrategyRuntimeState:
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        resume = store.load_resume_state()
        assert resume is not None
        return resume.strategy
    finally:
        store.close()


def seed_recorded_exposure(config_path: Path) -> None:
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        for order_id, broker, symbol, side, quantity in (
            (
                "entry-binance",
                BrokerName.BINANCE_TSM,
                config.live.binance_symbol,
                OrderSide.SELL,
                100.0,
            ),
            (
                "entry-fubon",
                BrokerName.FUBON_QFF,
                "QFFG6",
                OrderSide.BUY,
                2.0,
            ),
        ):
            request = OrderRequest(
                broker=broker,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=1.0,
                timestamp=ts(),
                row_index=0,
                qff_symbol="QFFG6",
            )
            store.record_order(
                OrderResult(order_id=order_id, request=request, status=OrderStatus.FILLED)
            )
            store.record_fill(
                Fill(
                    fill_id=f"fill-{order_id}",
                    order_id=order_id,
                    broker=broker,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=1.0,
                    fee_twd=0.0,
                    timestamp=ts(),
                    row_index=0,
                    qff_symbol="QFFG6",
                )
            )
        store.commit()
    finally:
        store.close()


def use_fake_brokers(monkeypatch, fake_case: str) -> None:
    builder = make_fake_broker_builder(fake_case)
    monkeypatch.setattr(
        commands_live,
        "build_reconciliation_brokers",
        builder,
    )
    monkeypatch.setattr(
        commands_recovery.helpers,
        "build_reconciliation_brokers",
        builder,
    )


# --- live-status ----------------------------------------------------------


def test_live_status_reports_no_state(tmp_path: Path, capsys) -> None:
    args = build_parser().parse_args(
        ["status", "live", "--config", str(write_config(tmp_path))]
    )
    assert command_live_status(args) == 0
    output = capsys.readouterr().out
    assert "strategy_state: none" in output


def test_live_status_reports_paused_position(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)

    args = build_parser().parse_args(["status", "live", "--config", str(config_path)])
    assert command_live_status(args) == 0

    output = capsys.readouterr().out
    assert "strategy_state: paused" in output
    assert "direction=short_tsm_long_qff" in output
    assert "qff_contracts=2" in output
    assert "ACTION: strategy is PAUSED" in output


# --- recover-manual-flat -------------------------------------------------


def test_recover_manual_flat_dry_run_does_not_change_state(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    seed_recorded_exposure(config_path)
    use_fake_brokers(monkeypatch, "matched")

    args = build_parser().parse_args(
        ["recover", "manual-flat", "--config", str(config_path), "--readonly"]
    )
    assert command_recover_manual_flat(args) == 0
    state = load_persisted_state(config_path)
    assert state.state == StrategyState.PAUSED
    assert state.tsm_units == -100.0
    assert state.qff_contracts == 2
    assert "Dry-run only" in capsys.readouterr().out


def test_recover_manual_flat_apply_offsets_ledger_and_remains_paused(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    seed_recorded_exposure(config_path)
    use_fake_brokers(monkeypatch, "matched")

    args = build_parser().parse_args(
        [
            "recover", "manual-flat",
            "--config",
            str(config_path),
            "--readonly",
            "--apply",
            "--reason",
            "test_manual_close",
        ]
    )
    assert command_recover_manual_flat(args) == 0

    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        resume = store.load_resume_state()
        assert resume is not None
        assert resume.strategy.state == StrategyState.PAUSED
        assert resume.strategy.position_direction is None
        assert resume.strategy.tsm_units == 0.0
        assert resume.strategy.qff_contracts == 0
        assert resume.strategy.pnl_status == "pending"
        assert store.load_pending_manual_close() is not None
        exposure = store.load_recorded_fill_exposure(
            tsm_symbol=config.live.binance_symbol,
            qff_symbol="QFFG6",
        )
        assert exposure[BrokerName.BINANCE_TSM] == 0.0
        assert exposure[BrokerName.FUBON_QFF] == 0.0
    finally:
        store.close()
    assert "strategy remains PAUSED" in capsys.readouterr().out


def test_recover_then_clear_pause_reaches_flat(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    seed_recorded_exposure(config_path)
    use_fake_brokers(monkeypatch, "matched")
    recover_args = build_parser().parse_args(
        [
            "recover", "manual-flat",
            "--config",
            str(config_path),
            "--readonly",
            "--apply",
            "--reason",
            "test_manual_close",
        ]
    )
    assert command_recover_manual_flat(recover_args) == 0

    clear_args = build_parser().parse_args(
        ["recover", "clear-pause", "--config", str(config_path), "--readonly"]
    )
    assert command_clear_pause(clear_args) == 0
    state = load_persisted_state(config_path)
    assert state.state == StrategyState.FLAT
    assert state.pnl_status == "pending"


def test_recover_manual_flat_refuses_nonflat_broker(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    use_fake_brokers(monkeypatch, "mismatch")
    args = build_parser().parse_args(
        ["recover", "manual-flat", "--config", str(config_path), "--readonly"]
    )
    assert command_recover_manual_flat(args) == 1
    assert load_persisted_state(config_path).tsm_units == -100.0


# --- clear-pause ----------------------------------------------------------


def test_clear_pause_matched_clears_to_open_with_position(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    use_fake_brokers(monkeypatch, "matched")

    args = build_parser().parse_args(["recover", "clear-pause", "--config", str(config_path)])
    assert command_clear_pause(args) == 0

    output = capsys.readouterr().out
    assert "Cleared PAUSED -> open" in output
    assert load_persisted_state(config_path).state == StrategyState.OPEN


def test_clear_pause_matched_clears_to_flat_without_position(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=False)
    use_fake_brokers(monkeypatch, "matched")

    args = build_parser().parse_args(["recover", "clear-pause", "--config", str(config_path)])
    assert command_clear_pause(args) == 0

    output = capsys.readouterr().out
    assert "Cleared PAUSED -> flat" in output
    assert load_persisted_state(config_path).state == StrategyState.FLAT


def test_clear_pause_mismatch_refuses_and_keeps_paused(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    use_fake_brokers(monkeypatch, "mismatch")

    args = build_parser().parse_args(["recover", "clear-pause", "--config", str(config_path)])
    assert command_clear_pause(args) == 1

    output = capsys.readouterr().out
    assert "Refusing clear-pause" in output
    # State must remain PAUSED when reconciliation does not match.
    assert load_persisted_state(config_path).state == StrategyState.PAUSED


def test_clear_pause_noop_when_not_paused(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.FLAT, with_position=False)
    use_fake_brokers(monkeypatch, "matched")

    args = build_parser().parse_args(["recover", "clear-pause", "--config", str(config_path)])
    assert command_clear_pause(args) == 0
    assert "nothing to clear" in capsys.readouterr().out


def test_clear_pause_without_readonly_refuses_real_brokers(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("LUX_READONLY_BROKER", raising=False)
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=False)

    args = build_parser().parse_args(["recover", "clear-pause", "--config", str(config_path)])
    try:
        command_clear_pause(args)
    except SystemExit as exc:
        assert "--readonly" in str(exc)
    else:
        raise AssertionError("Expected SystemExit without --readonly")
