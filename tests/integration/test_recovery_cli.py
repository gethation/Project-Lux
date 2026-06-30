from __future__ import annotations

from datetime import datetime
from pathlib import Path

from lux_trader.cli import (
    build_parser,
    command_binance_manual_close,
    command_clear_pause,
    command_live_status,
)
from lux_trader.config import load_config
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.models import Direction, StrategyState
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.store import SQLiteStore


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


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


# --- live-status ----------------------------------------------------------


def test_live_status_reports_no_state(tmp_path: Path, capsys) -> None:
    args = build_parser().parse_args(
        ["live-status", "--config", str(write_config(tmp_path))]
    )
    assert command_live_status(args) == 0
    output = capsys.readouterr().out
    assert "strategy_state: none" in output


def test_live_status_reports_paused_position(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)

    args = build_parser().parse_args(["live-status", "--config", str(config_path)])
    assert command_live_status(args) == 0

    output = capsys.readouterr().out
    assert "strategy_state: paused" in output
    assert "direction=short_tsm_long_qff" in output
    assert "qff_contracts=2" in output
    assert "ACTION: strategy is PAUSED" in output


# --- clear-pause ----------------------------------------------------------


def test_clear_pause_matched_clears_to_open_with_position(
    tmp_path: Path, capsys
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)

    args = build_parser().parse_args(
        ["clear-pause", "--config", str(config_path), "--fake", "--fake-case", "matched"]
    )
    assert command_clear_pause(args) == 0

    output = capsys.readouterr().out
    assert "Cleared PAUSED -> open" in output
    assert load_persisted_state(config_path).state == StrategyState.OPEN


def test_clear_pause_matched_clears_to_flat_without_position(
    tmp_path: Path, capsys
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=False)

    args = build_parser().parse_args(
        ["clear-pause", "--config", str(config_path), "--fake", "--fake-case", "matched"]
    )
    assert command_clear_pause(args) == 0

    output = capsys.readouterr().out
    assert "Cleared PAUSED -> flat" in output
    assert load_persisted_state(config_path).state == StrategyState.FLAT


def test_clear_pause_mismatch_refuses_and_keeps_paused(
    tmp_path: Path, capsys
) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)

    args = build_parser().parse_args(
        ["clear-pause", "--config", str(config_path), "--fake", "--fake-case", "mismatch"]
    )
    assert command_clear_pause(args) == 1

    output = capsys.readouterr().out
    assert "Refusing clear-pause" in output
    # State must remain PAUSED when reconciliation does not match.
    assert load_persisted_state(config_path).state == StrategyState.PAUSED


def test_clear_pause_noop_when_not_paused(tmp_path: Path, capsys) -> None:
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.FLAT, with_position=False)

    args = build_parser().parse_args(
        ["clear-pause", "--config", str(config_path), "--fake"]
    )
    assert command_clear_pause(args) == 0
    assert "nothing to clear" in capsys.readouterr().out


# --- binance-manual-close gating -----------------------------------------


def test_binance_manual_close_requires_allow_live_order(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "binance-manual-close",
            "--config",
            str(write_config(tmp_path, allow_live_order=False)),
            "--symbol",
            "TSM/USDT:USDT",
            "--side",
            "buy",
            "--quantity",
            "0.02",
            "--confirm-symbol",
            "TSM/USDT:USDT",
        ]
    )
    try:
        command_binance_manual_close(args)
    except SystemExit as exc:
        assert "allow_live_order" in str(exc)
    else:
        raise AssertionError("Expected SystemExit when allow_live_order is false")


def test_binance_manual_close_requires_env_gates(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ("PROJECT_LUX_ALLOW_LIVE_ORDER", "BINANCE_ALLOW_LIVE_ORDER", "LUX_BINANCE_MANUAL_CLOSE"):
        monkeypatch.delenv(name, raising=False)
    args = build_parser().parse_args(
        [
            "binance-manual-close",
            "--config",
            str(write_config(tmp_path, allow_live_order=True)),
            "--symbol",
            "TSM/USDT:USDT",
            "--side",
            "buy",
            "--quantity",
            "0.02",
            "--confirm-symbol",
            "TSM/USDT:USDT",
        ]
    )
    try:
        command_binance_manual_close(args)
    except SystemExit as exc:
        assert "gates closed" in str(exc)
    else:
        raise AssertionError("Expected SystemExit when manual-close env gates are closed")
