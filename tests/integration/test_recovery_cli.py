from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

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

from fakes import make_fake_broker_builder


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


def write_config(tmp_path: Path, *, allow_live_order: bool = False) -> Path:
    config_path = tmp_path / "config.test.toml"
    store_path = (tmp_path / "project_lux.sqlite3").as_posix()
    cache_dir = (tmp_path / "taifex_cache").as_posix()
    config_path.write_text(
        "\n".join(
            [
                "[fees]",
                "ccf_contract_multiplier = 2000.0",
                "",
                "[paths]",
                "input_csv = ''",
                f"store_path = '{store_path}'",
                "",
                "[safety]",
                f"allow_live_order = {str(allow_live_order).lower()}",
                "",
                "[live_market_data]",
                "ccf_symbol = 'CCFG6'",
                "umc_symbol = 'UMC'",
                f"taifex_cache_dir = '{cache_dir}'",
                "",
                "[broker_reconciliation]",
                "enabled = false",
                "fail_on_mismatch = false",
                "umc_units_tolerance = 0.000001",
                "ccf_contract_tolerance = 0",
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
            runtime.position_direction = Direction.SHORT_UMC_LONG_CCF
            runtime.umc_units = -100.0
            runtime.ccf_contracts = 2
            runtime.trading_ccf_symbol = "CCFG6"
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
                "entry-ibkr",
                BrokerName.IBKR_UMC,
                config.live.umc_symbol,
                OrderSide.SELL,
                100.0,
            ),
            (
                "entry-fubon",
                BrokerName.FUBON_CCF,
                "CCFG6",
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
                ccf_symbol="CCFG6",
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
                    ccf_symbol="CCFG6",
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
    assert "direction=short_umc_long_ccf" in output
    assert "ccf_contracts=2" in output
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
    assert state.umc_units == -100.0
    assert state.ccf_contracts == 2
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
        assert resume.strategy.umc_units == 0.0
        assert resume.strategy.ccf_contracts == 0
        assert resume.strategy.pnl_status == "pending"
        assert store.load_pending_manual_close() is not None
        exposure = store.load_recorded_fill_exposure(
            umc_symbol=config.live.umc_symbol,
            ccf_symbol="CCFG6",
        )
        assert exposure[BrokerName.IBKR_UMC] == 0.0
        assert exposure[BrokerName.FUBON_CCF] == 0.0
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
    assert load_persisted_state(config_path).umc_units == -100.0


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


def seed_ccf_exit_already_filled(config_path: Path) -> None:
    """The CCF leg's exit filled; the UMC leg's did not.

    This is what a half-completed exit leaves behind, and it is the only state
    manual-flat is ever reached from. The strategy still believes it holds both
    legs, because the exit never completed, but the ledger has already recorded
    the CCF side of it.
    """
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        request = OrderRequest(
            broker=BrokerName.FUBON_CCF,
            symbol="CCFG6",
            side=OrderSide.SELL,
            quantity=2.0,
            price=1.0,
            timestamp=ts(),
            row_index=1,
            ccf_symbol="CCFG6",
        )
        store.record_order(
            OrderResult(order_id="exit-fubon", request=request, status=OrderStatus.FILLED)
        )
        store.record_fill(
            Fill(
                fill_id="fill-exit-fubon",
                order_id="exit-fubon",
                broker=BrokerName.FUBON_CCF,
                symbol="CCFG6",
                side=OrderSide.SELL,
                quantity=2.0,
                price=1.0,
                fee_twd=0.0,
                timestamp=ts(),
                row_index=1,
                ccf_symbol="CCFG6",
            )
        )
        store.commit()
    finally:
        store.close()


def test_manual_flat_squares_the_ledger_not_the_strategy(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """REGRESSION 2026-08-19: the adjustment mirrored the strategy state, which
    disagrees with the ledger exactly when a pair half-completes -- the only
    case this command exists for.

    The CCF exit had filled, so the ledger was already square on CCF while the
    strategy still counted the lot. Writing -state.ccf_contracts put a second
    -2 onto a balanced ledger, leaving recorded exposure at -2 against a flat
    broker. clear-pause then refused forever with recorded_fill_position_mismatch
    and no tool could undo it.
    """
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    seed_recorded_exposure(config_path)
    seed_ccf_exit_already_filled(config_path)
    use_fake_brokers(monkeypatch, "matched")

    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        before = store.load_recorded_fill_exposure(
            umc_symbol=config.live.umc_symbol, ccf_symbol="CCFG6"
        )
    finally:
        store.close()
    # The ledger and the strategy disagree: CCF is square, UMC is not.
    assert before[BrokerName.FUBON_CCF] == 0.0
    assert before[BrokerName.IBKR_UMC] == -100.0

    args = build_parser().parse_args(
        [
            "recover", "manual-flat",
            "--config", str(config_path),
            "--readonly", "--apply",
            "--reason", "half completed exit",
        ]
    )
    assert command_recover_manual_flat(args) == 0

    out = capsys.readouterr().out
    assert "fubon_adjustment=0" in out, out
    assert "umc_adjustment=100" in out, out

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        after = store.load_recorded_fill_exposure(
            umc_symbol=config.live.umc_symbol, ccf_symbol="CCFG6"
        )
    finally:
        store.close()
    # Both legs now agree with the flat brokers, so clear-pause can proceed.
    assert after[BrokerName.IBKR_UMC] == 0.0
    assert after[BrokerName.FUBON_CCF] == 0.0


def test_manual_flat_re_squares_a_ledger_an_earlier_recovery_left_off(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """REGRESSION 2026-08-19: the first recovery wrote an adjustment derived
    from the strategy and left recorded exposure at CCF -1 against a flat
    broker. clear-pause then refused forever, and re-running manual-flat just
    printed 'already applied' and returned 0 -- the operator had no tool that
    could correct it.
    """
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    seed_recorded_exposure(config_path)
    use_fake_brokers(monkeypatch, "matched")

    apply_args = build_parser().parse_args(
        [
            "recover", "manual-flat", "--config", str(config_path),
            "--readonly", "--apply", "--reason", "first recovery",
        ]
    )
    assert command_recover_manual_flat(apply_args) == 0
    capsys.readouterr()

    config = load_config(config_path)
    # Simulate the damage the old formula did: a second, wrong adjustment.
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        pending = store.load_pending_manual_close()
        assert pending is not None
        store.record_manual_flat_ledger_repair(
            recovery_id=str(pending["recovery_id"]),
            created_at=ts(),
            ccf_symbol="CCFG6",
            umc_symbol=config.live.umc_symbol,
            umc_adjustment=0.0,
            ccf_adjustment=-2.0,
            reason="simulating the old strategy-derived adjustment",
        )
        store.commit()
        off = store.load_recorded_fill_exposure(
            umc_symbol=config.live.umc_symbol, ccf_symbol="CCFG6"
        )
    finally:
        store.close()
    assert off[BrokerName.FUBON_CCF] == -2.0

    # Re-running now detects the residual instead of waving it past.
    repair_args = build_parser().parse_args(
        [
            "recover", "manual-flat", "--config", str(config_path),
            "--readonly", "--apply", "--reason", "re-square the ledger",
        ]
    )
    assert command_recover_manual_flat(repair_args) == 0
    out = capsys.readouterr().out
    assert "already applied but the ledger is NOT square" in out, out
    assert "Ledger repair applied" in out, out

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        after = store.load_recorded_fill_exposure(
            umc_symbol=config.live.umc_symbol, ccf_symbol="CCFG6"
        )
        # No second pending close was opened; the position was already settled.
        rows = store.connection.execute(
            "SELECT COUNT(*) c FROM pending_manual_closes"
        ).fetchone()
    finally:
        store.close()
    assert after[BrokerName.FUBON_CCF] == 0.0
    assert after[BrokerName.IBKR_UMC] == 0.0
    assert rows["c"] == 1


def test_manual_flat_on_a_square_ledger_is_still_a_no_op(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    """The re-square path must not become a reason to write adjustments every
    time the command is run."""
    config_path = write_config(tmp_path)
    seed_state(config_path, state=StrategyState.PAUSED, with_position=True)
    seed_recorded_exposure(config_path)
    use_fake_brokers(monkeypatch, "matched")

    args = build_parser().parse_args(
        [
            "recover", "manual-flat", "--config", str(config_path),
            "--readonly", "--apply", "--reason", "first recovery",
        ]
    )
    assert command_recover_manual_flat(args) == 0
    capsys.readouterr()

    assert command_recover_manual_flat(args) == 0
    out = capsys.readouterr().out
    assert "already applied" in out
    assert "ledger square" in out
    assert "Ledger repair applied" not in out


# --- settle-manual-close --------------------------------------------------
#
# Shape of the 2026-08-19 incident: both entry legs filled, the CCF exit
# filled, the UMC exit was closed by hand outside the system, and manual-flat
# already squared the ledger and left pnl_status pending.

ENTRY_ROW = 10
EXIT_ROW = 20
ENTRY_UMC_PRICE_USD = 18.463926
ENTRY_UMC_TWD_FAIR = 117.7922973  # x5 = 588.9615 TWD/ADR at usd_twd 31.898
ENTRY_UMC_FEE_TWD = 71.3724624783318
SETTLE_FX = 31.85683
REALIZED_USD = 49.540918


def settle_open_trade() -> dict:
    return {
        "entry_signal_idx": ENTRY_ROW,
        "entry_signal_time": "2026-02-01T22:18:00+08:00",
        "entry_signal_zscore": 1.14,
        "entry_idx": ENTRY_ROW,
        "entry_time": "2026-02-01T22:18:00+08:00",
        "entry_delay_minutes": 0,
        "entry_fill_zscore": 1.14,
        "direction": "short_umc_long_ccf",
        "entry_umc_twd_fair": ENTRY_UMC_TWD_FAIR,
        # The BAR price. The recorded fill below is 116.5 -- they differ, which
        # is the point of test_settle_manual_close_prices_ccf_from_the_fill.
        "entry_ccf_close": 116.25,
        "entry_fill_price_type": "close",
        "umc_units": -394.0,
        "ccf_units": 2000.0,
        "ccf_contracts": 1,
        "raw_ccf_contracts": 1.0,
        "leg_notional_twd": 1000000.0,
        "actual_leg_notional_twd": 233000.0,
        "ccf_contract_multiplier": 2000.0,
        "entry_umc_fee_twd": ENTRY_UMC_FEE_TWD,
        "entry_ccf_fee_twd": 88.0,
        "entry_ccf_tax_twd": 5,
        "entry_fee_twd": 164.37246247833178,
        "ccf_symbol": "CCFG6",
        "ccf_expiry": "2026-09-16",
        "contract_policy_state": "active",
    }


def record_settle_fill(
    store: SQLiteStore,
    *,
    order_id: str,
    broker: BrokerName,
    symbol: str,
    side: OrderSide,
    quantity: float,
    price: float,
    fee_twd: float,
    row_index: int,
) -> None:
    request = OrderRequest(
        broker=broker,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        timestamp=ts(),
        row_index=row_index,
        ccf_symbol="CCFG6",
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
            price=price,
            fee_twd=fee_twd,
            timestamp=ts(),
            row_index=row_index,
            ccf_symbol="CCFG6",
        )
    )


def seed_pending_settlement(
    config_path: Path,
    *,
    with_ccf_exit_fill: bool = True,
    with_fx_tick: bool = True,
    still_holding: bool = False,
) -> str:
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        record_settle_fill(
            store,
            order_id="entry-ibkr",
            broker=BrokerName.IBKR_UMC,
            symbol=config.live.umc_symbol,
            side=OrderSide.SELL,
            quantity=394.0,
            price=ENTRY_UMC_PRICE_USD,
            fee_twd=ENTRY_UMC_FEE_TWD,
            row_index=ENTRY_ROW,
        )
        record_settle_fill(
            store,
            order_id="entry-fubon",
            broker=BrokerName.FUBON_CCF,
            symbol="CCFG6",
            side=OrderSide.BUY,
            quantity=1.0,
            price=116.5,
            fee_twd=93.0,
            row_index=ENTRY_ROW,
        )
        if with_ccf_exit_fill:
            record_settle_fill(
                store,
                order_id="exit-fubon",
                broker=BrokerName.FUBON_CCF,
                symbol="CCFG6",
                side=OrderSide.SELL,
                quantity=1.0,
                price=116.5,
                fee_twd=93.0,
                row_index=EXIT_ROW - 1,
            )
        if with_fx_tick:
            store.connection.execute(
                "INSERT INTO market_ticks ("
                " observed_at, source, symbol, quote_timestamp, price,"
                " bid, ask, raw_json"
                ") VALUES (?, 'twelvedata', 'USD/TWD', ?, ?, NULL, NULL, '{}')",
                (ts().isoformat(), ts().isoformat(), SETTLE_FX),
            )

        original = StrategyRuntimeState(state=StrategyState.PAUSED)
        original.position_direction = Direction.SHORT_UMC_LONG_CCF
        original.umc_units = -394.0
        original.ccf_units = 2000.0
        original.ccf_contracts = 1
        original.trading_ccf_symbol = "CCFG6"
        original.realized_pnl = 409.6384113152398
        original.realized_fee_twd = 486.2660486847582
        original.open_trade = settle_open_trade()
        store.record_manual_flat_recovery(
            recovery_id="manual-flat-test",
            created_at=ts(),
            row_index=EXIT_ROW,
            ccf_symbol="CCFG6",
            umc_symbol=config.live.umc_symbol,
            umc_adjustment=394.0,
            ccf_adjustment=0.0,
            reason="test",
            original_state=original,
        )

        settled = StrategyRuntimeState(state=StrategyState.PAUSED)
        settled.realized_pnl = original.realized_pnl
        settled.realized_fee_twd = original.realized_fee_twd
        settled.trading_ccf_symbol = "CCFG6"
        settled.pnl_status = "pending"
        if still_holding:
            settled.position_direction = Direction.SHORT_UMC_LONG_CCF
            settled.umc_units = -394.0
            settled.ccf_contracts = 1
        store.save_state(EXIT_ROW, ts(), settled, IndicatorEngine(window=500))
        store.commit()
    finally:
        store.close()
    return "manual-flat-test"


class FakePnlBroker:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.closed = False

    def fetch_realized_pnl(self) -> dict:
        return self.payload

    def close(self) -> None:
        self.closed = True


def realized_payload(
    realized: float | None = REALIZED_USD,
    *,
    ledger: float | None = REALIZED_USD,
) -> dict:
    return {
        "accounts": ["U00000001"],
        "pnl": [
            {
                "account": "U00000001",
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": 0.0,
                "daily_pnl_usd": 58.0718,
            }
        ],
        "ledger_realized": {} if ledger is None else {"USD": ledger},
        "fetched_at": ts(),
    }


def use_fake_pnl_broker(monkeypatch, payload: dict) -> FakePnlBroker:
    broker = FakePnlBroker(payload)
    monkeypatch.setattr(
        commands_recovery.helpers,
        "build_umc_readonly_broker",
        lambda config, *, readonly: broker,
    )
    return broker


def settle_args(config_path: Path, *extra: str):
    return build_parser().parse_args(
        ["recover", "settle-manual-close", "--config", str(config_path), *extra]
    )


def load_pending_row(config_path: Path) -> dict | None:
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        row = store.connection.execute(
            "SELECT * FROM pending_manual_closes "
            "WHERE recovery_id = 'manual-flat-test'"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        store.close()


def load_trades(config_path: Path) -> list[dict]:
    config = load_config(config_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        return [dict(row) for row in store.connection.execute("SELECT * FROM trades")]
    finally:
        store.close()


def test_settle_manual_close_books_the_brokers_number_exactly(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    args = settle_args(
        config_path, "--from-broker", "--readonly", "--apply", "--reason", "test"
    )
    assert commands_recovery.command_settle_manual_close(args) == 0

    trades = load_trades(config_path)
    assert len(trades) == 1
    trade = trades[0]
    # The whole point of the broker basis: the UMC leg's NET equals IBKR's own
    # figure, whatever the fee model would have guessed.
    umc_leg_net = trade["umc_pnl"] - ENTRY_UMC_FEE_TWD - trade["exit_umc_fee_twd"]
    assert umc_leg_net == pytest.approx(REALIZED_USD * SETTLE_FX, abs=1e-6)

    state = load_persisted_state(config_path)
    assert state.pnl_status == "complete"
    assert state.realized_pnl == pytest.approx(
        409.6384113152398 + trade["gross_pnl_twd"] - trade["exit_fee_twd"]
    )

    pending = load_pending_row(config_path)
    assert pending["status"] == "settled"
    assert pending["settled_at"] is not None
    settlement = json.loads(pending["settlement_json"])
    assert settlement["basis"] == "broker_realized"
    assert settlement["realized_usd"] == pytest.approx(REALIZED_USD)


def test_settle_manual_close_dry_run_changes_nothing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    args = settle_args(config_path, "--from-broker", "--readonly")
    assert commands_recovery.command_settle_manual_close(args) == 0

    assert "Dry-run only" in capsys.readouterr().out
    assert load_trades(config_path) == []
    assert load_persisted_state(config_path).pnl_status == "pending"
    assert load_pending_row(config_path)["status"] == "pending"


def test_settle_manual_close_refuses_a_second_settlement(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())
    apply_args = settle_args(
        config_path, "--from-broker", "--readonly", "--apply", "--reason", "test"
    )
    assert commands_recovery.command_settle_manual_close(apply_args) == 0

    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.command_settle_manual_close(
            settle_args(
                config_path,
                "--from-broker",
                "--readonly",
                "--apply",
                "--reason",
                "again",
            )
        )
    assert "No pending manual close" in str(excinfo.value)
    assert len(load_trades(config_path)) == 1


def test_settle_manual_close_refuses_while_the_strategy_still_holds(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path, still_holding=True)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.command_settle_manual_close(
            settle_args(config_path, "--from-broker", "--readonly")
        )
    assert "manual-flat" in str(excinfo.value)


def test_settle_manual_close_refuses_a_price_that_contradicts_the_broker(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    args = settle_args(
        config_path,
        "--from-broker",
        "--readonly",
        "--umc-exit-price",
        "17.10",
        "--apply",
        "--reason",
        "test",
    )
    assert commands_recovery.command_settle_manual_close(args) == 1
    assert "disagree" in capsys.readouterr().out
    assert load_trades(config_path) == []
    assert load_persisted_state(config_path).pnl_status == "pending"


def test_settle_manual_close_accepts_a_price_that_matches_the_broker(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    config = load_config(config_path)
    implied = commands_recovery.build_settlement(
        config=config,
        open_trade=settle_open_trade(),
        umc_units=-394.0,
        ccf_units=2000.0,
        ccf_contracts=1,
        entry_umc_price_usd=ENTRY_UMC_PRICE_USD,
        entry_ccf_price=116.5,
        entry_ccf_source="test",
        ccf_exit_price=116.5,
        usd_twd=SETTLE_FX,
        realized_usd=REALIZED_USD,
        umc_exit_price=None,
        price_tolerance_usd=0.05,
    )["umc_exit_price_implied_usd"]

    args = settle_args(
        config_path,
        "--from-broker",
        "--readonly",
        "--umc-exit-price",
        f"{implied:.6f}",
        "--apply",
        "--reason",
        "test",
    )
    assert commands_recovery.command_settle_manual_close(args) == 0
    assert "price cross-check" in capsys.readouterr().out
    assert len(load_trades(config_path)) == 1


def test_settle_manual_close_prices_ccf_from_the_fill_not_the_bar(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    args = settle_args(
        config_path, "--from-broker", "--readonly", "--apply", "--reason", "test"
    )
    assert commands_recovery.command_settle_manual_close(args) == 0

    output = capsys.readouterr().out
    assert "the strategy booked its entry at the bar price 116.25" in output
    trade = load_trades(config_path)[0]
    # Entry and exit fills were both 116.5, so the leg made nothing. Pricing
    # against the bar's 116.25 would have invented +500 TWD.
    assert trade["ccf_pnl"] == pytest.approx(0.0)
    assert trade["entry_ccf_close"] == pytest.approx(116.5)


def test_settle_manual_close_works_from_a_supplied_price_with_no_broker(
    tmp_path: Path, capsys
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)

    args = settle_args(
        config_path, "--umc-exit-price", "18.3276", "--apply", "--reason", "test"
    )
    assert commands_recovery.command_settle_manual_close(args) == 0

    pending = load_pending_row(config_path)
    settlement = json.loads(pending["settlement_json"])
    assert settlement["basis"] == "model"
    assert settlement["realized_usd"] is None
    assert load_persisted_state(config_path).pnl_status == "complete"


def test_settle_manual_close_needs_a_source(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.command_settle_manual_close(settle_args(config_path))
    assert "Nothing to settle from" in str(excinfo.value)


def test_settle_manual_close_uses_the_recorded_fx_tick(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    assert (
        commands_recovery.command_settle_manual_close(
            settle_args(config_path, "--from-broker", "--readonly")
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"{SETTLE_FX:.5f}" in output
    assert "recorded tick" in output


def test_settle_manual_close_refuses_without_a_usable_fx_rate(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path, with_fx_tick=False)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.command_settle_manual_close(
            settle_args(config_path, "--from-broker", "--readonly")
        )
    assert "--usd-twd" in str(excinfo.value)


def test_settle_manual_close_refuses_without_a_ccf_exit_price(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path, with_ccf_exit_fill=False)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.command_settle_manual_close(
            settle_args(config_path, "--from-broker", "--readonly")
        )
    assert "--ccf-exit-price" in str(excinfo.value)


def test_settle_manual_close_refuses_a_contradicted_ccf_exit_price(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = write_config(tmp_path)
    seed_pending_settlement(config_path)
    use_fake_pnl_broker(monkeypatch, realized_payload())

    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.command_settle_manual_close(
            settle_args(
                config_path, "--from-broker", "--readonly", "--ccf-exit-price", "120"
            )
        )
    assert "contradicts the recorded CCF exit fill" in str(excinfo.value)


# --- realized-PnL payload guards ------------------------------------------


def test_single_account_realized_refuses_multiple_accounts() -> None:
    payload = realized_payload()
    payload["pnl"].append(dict(payload["pnl"][0], account="U00000002"))
    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.single_account_realized_usd(payload)
    assert "accounts" in str(excinfo.value)


def test_single_account_realized_refuses_a_ledger_disagreement() -> None:
    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.single_account_realized_usd(
            realized_payload(49.54, ledger=101.0)
        )
    assert "disagree on realized PnL" in str(excinfo.value)


def test_single_account_realized_refuses_an_unpopulated_subscription() -> None:
    with pytest.raises(SystemExit) as excinfo:
        commands_recovery.single_account_realized_usd(realized_payload(None))
    assert "never updated" in str(excinfo.value)
