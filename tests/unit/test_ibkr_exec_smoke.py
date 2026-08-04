"""A tiny real round trip through the IBKR order path.

What it proves: placement, the three-tier fill confirmation, whole-share
rounding, and that the position actually moves.

What it does NOT prove, which matters more than what it does:

  It builds its own plan, so strategy -> price_policy -> validator is never
  touched. That is the path that rejected every entry on 2026-08-04 while the
  whole suite stayed green.

  It is one leg at one venue, so it cannot produce the state the pair fears --
  CCF filled and UMC unknown. A clean round trip is the happy path only.

These tests therefore concentrate on the failure exits: a half-completed round
trip must be loud and must say how to flatten.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import lux_trader.cli.commands_execution as cli_module
from lux_trader.cli.parser import build_parser
from lux_trader.core.models import OrderSide
from lux_trader.execution.outcome import ExecutionOutcome, ExecutionOutcomeStatus


def write_config(tmp_path: Path, *, allow_live_order: bool = True) -> Path:
    path = tmp_path / "config.test.toml"
    path.write_text(
        "\n".join(
            [
                "[fees]",
                "ccf_contract_multiplier = 2000.0",
                "",
                "[paths]",
                "input_csv = ''",
                f"store_path = '{(tmp_path / 'store.sqlite3').as_posix()}'",
                "",
                "[safety]",
                f"allow_live_order = {str(allow_live_order).lower()}",
                "",
                "[live_market_data]",
                "ccf_symbol = 'CCFH6'",
                "umc_symbol = 'UMC'",
                "fubon_env_path = '.env'",
                f"taifex_cache_dir = '{(tmp_path / 'taifex').as_posix()}'",
            ]
        ),
        encoding="utf-8",
    )
    return path


class FakeIbkrAdapter:
    def __init__(
        self,
        *,
        positions: tuple[float, ...] = (0.0,),
        open_orders: tuple[dict, ...] = (),
        open_status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.FILLED,
        close_status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.FILLED,
        open_filled: float | None = None,
    ) -> None:
        self.positions = list(positions)
        self.open_orders = open_orders
        self.open_status = open_status
        self.close_status = close_status
        self.open_filled = open_filled
        self.executed = []
        self.closed = False
        self._reads = 0

    def _next_position(self) -> float:
        index = min(self._reads, len(self.positions) - 1)
        self._reads += 1
        return self.positions[index]

    def fetch_position_quantity(self) -> float:
        return self._next_position()

    def fetch_open_orders(self):
        return self.open_orders

    def preflight(self):
        return SimpleNamespace(
            position_quantity=self._next_position(), open_orders=self.open_orders
        )

    def execute(self, plan):
        self.executed.append(plan)
        leg = plan.legs[0]
        first = len(self.executed) == 1
        status = self.open_status if first else self.close_status
        quantity = leg.quantity
        if first and self.open_filled is not None:
            quantity = self.open_filled
        fills = ()
        if status == ExecutionOutcomeStatus.FILLED and quantity > 0:
            fills = (SimpleNamespace(quantity=quantity),)
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=status,
            message="fake",
            fills=fills,
        )

    def close(self) -> None:
        self.closed = True


def install(monkeypatch, adapter) -> None:
    monkeypatch.setattr(
        cli_module, "IbkrUmcExecutionAdapter", lambda *a, **k: adapter
    )
    monkeypatch.setattr(cli_module.time, "sleep", lambda *_: None)


def gates_open(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("IBKR_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_IBKR_EXECUTION_SMOKE", "1")


def parse(tmp_path: Path, **overrides):
    config = overrides.pop("config", None) or write_config(tmp_path)
    argv = [
        "admin", "exec-smoke", "--venue", "ibkr",
        "--config", str(config),
        "--confirm-symbol", "UMC",
    ]
    if "shares" in overrides:
        argv += ["--shares", str(overrides.pop("shares"))]
    if "side" in overrides:
        argv += ["--side", overrides.pop("side")]
    return build_parser().parse_args(argv)


# --- gates -------------------------------------------------------------------


def test_smoke_needs_its_own_gate_beyond_the_live_order_gates(
    tmp_path, monkeypatch
) -> None:
    """Enabling live orders for the trading loop must not also enable a tool
    whose entire purpose is to open a position for no trading reason."""
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("IBKR_ALLOW_LIVE_ORDER", "1")
    monkeypatch.delenv("LUX_IBKR_EXECUTION_SMOKE", raising=False)
    install(monkeypatch, FakeIbkrAdapter())

    with pytest.raises(SystemExit, match="LUX_IBKR_EXECUTION_SMOKE"):
        cli_module.command_exec_smoke(parse(tmp_path))


def test_smoke_needs_the_live_order_gates_too(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LUX_IBKR_EXECUTION_SMOKE", "1")
    monkeypatch.delenv("PROJECT_LUX_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.delenv("IBKR_ALLOW_LIVE_ORDER", raising=False)
    install(monkeypatch, FakeIbkrAdapter())

    with pytest.raises(SystemExit, match="gates closed"):
        cli_module.command_exec_smoke(parse(tmp_path))


def test_smoke_needs_allow_live_order(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    install(monkeypatch, FakeIbkrAdapter())
    config = write_config(tmp_path, allow_live_order=False)

    with pytest.raises(SystemExit, match="allow_live_order"):
        cli_module.command_exec_smoke(parse(tmp_path, config=config))


def test_the_confirm_symbol_gate_is_actually_enforced(tmp_path, monkeypatch) -> None:
    """REGRESSION: --confirm-symbol was required by the parser and read by
    nothing on this path, so the operator's deliberate second confirmation was
    accepted and discarded on the one command that opens a real position."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter()
    install(monkeypatch, adapter)
    args = build_parser().parse_args(
        [
            "admin", "exec-smoke", "--venue", "ibkr",
            "--config", str(write_config(tmp_path)),
            "--confirm-symbol", "TOTALLY-WRONG",
        ]
    )

    with pytest.raises(SystemExit, match="confirm-symbol"):
        cli_module.command_exec_smoke(args)

    assert adapter.executed == []


def test_fubon_exec_smoke_refuses_a_side_it_would_ignore(tmp_path, monkeypatch) -> None:
    """`--side sell` used to be an argparse error here. Once the flag moved to
    the shared subparser it parsed, and the Fubon path hard-codes BUY -- so the
    operator got a real LONG where they asked for a short."""
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_FUBON_EXECUTION_SMOKE", "1")
    args = build_parser().parse_args(
        [
            "admin", "exec-smoke", "--venue", "fubon",
            "--config", str(write_config(tmp_path)),
            "--symbol", "CCFH6", "--lot", "1",
            "--side", "sell",
            "--confirm-symbol", "CCFH6",
        ]
    )

    with pytest.raises(SystemExit, match="--side is not supported"):
        cli_module.command_exec_smoke(args)


def test_a_fill_sum_with_float_error_rounds_rather_than_truncating(
    tmp_path, monkeypatch
) -> None:
    """Fills are whole shares, but summing several as floats can land just
    under. int() on 2.9999999996 closes 2 and strands a share, which then trips
    the CRITICAL check and prints a `--shares 0` remediation command.

    (Not tested with 2.5: round() is banker's rounding, so 2.5 -> 2. A genuine
    half-share fill cannot happen on a whole-share equity, and pinning that
    behaviour would assert an accident.)
    """
    gates_open(monkeypatch)

    class FractionalAdapter(FakeIbkrAdapter):
        def execute(self, plan):
            self.executed.append(plan)
            first = len(self.executed) == 1
            return ExecutionOutcome(
                plan_id=plan.plan_id,
                timestamp=plan.timestamp,
                status=ExecutionOutcomeStatus.FILLED,
                message="fake",
                fills=(
                    SimpleNamespace(
                        quantity=2.9999999996 if first else plan.legs[0].quantity
                    ),
                ),
            )

    adapter = FractionalAdapter(positions=(0.0, 3.0, 0.0))
    install(monkeypatch, adapter)

    assert cli_module.command_exec_smoke(parse(tmp_path, shares=3)) == 0
    assert adapter.executed[1].legs[0].quantity == 3.0


def test_the_flatten_hint_rounds_the_share_count(capsys) -> None:
    """A residual of 2.9999999996 must print 3; int() would print 2 and leave a
    share naked in a command the operator copy-pastes during an incident."""
    cli_module.print_ibkr_manual_close_hint("UMC", 2.9999999996)
    assert "--shares 3" in capsys.readouterr().out

    cli_module.print_ibkr_manual_close_hint("UMC", -2.9999999996)
    assert "--side buy --shares 3" in capsys.readouterr().out


def test_the_flatten_hint_says_so_when_the_position_is_unreadable(capsys) -> None:
    cli_module.print_ibkr_manual_close_hint("UMC", None)
    out = capsys.readouterr().out
    assert "Cannot print a flatten command" in out
    assert "--shares <n>" in out


# --- preflight ---------------------------------------------------------------


def test_refuses_to_start_from_a_nonzero_position(tmp_path, monkeypatch) -> None:
    """A round trip from a nonzero start cannot tell its own fills from what
    was already there."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(positions=(250.0,))
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="nonzero position"):
        cli_module.command_exec_smoke(parse(tmp_path))

    assert adapter.executed == []


def test_refuses_to_start_with_open_orders(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(open_orders=({"order_id": 7},))
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="existing open orders"):
        cli_module.command_exec_smoke(parse(tmp_path))

    assert adapter.executed == []


# --- the round trip ----------------------------------------------------------


def test_default_is_one_share_and_a_long_round_trip(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(positions=(0.0, 1.0, 0.0))
    install(monkeypatch, adapter)

    assert cli_module.command_exec_smoke(parse(tmp_path)) == 0

    open_leg, close_leg = (p.legs[0] for p in adapter.executed)
    assert open_leg.side == OrderSide.BUY and open_leg.quantity == 1.0
    assert close_leg.side == OrderSide.SELL and close_leg.quantity == 1.0
    assert adapter.closed is True


def test_the_short_round_trip_opens_by_selling(tmp_path, monkeypatch) -> None:
    """The path borrow makes different, and which nothing has ever exercised."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(positions=(0.0, -1.0, 0.0))
    install(monkeypatch, adapter)

    assert cli_module.command_exec_smoke(parse(tmp_path, side="sell")) == 0

    open_leg, close_leg = (p.legs[0] for p in adapter.executed)
    assert open_leg.side == OrderSide.SELL
    assert close_leg.side == OrderSide.BUY


def test_the_close_uses_the_filled_quantity_not_the_requested_one(
    tmp_path, monkeypatch
) -> None:
    """A partial open closed at the requested size would flip the position."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(positions=(0.0, 3.0, 0.0), open_filled=3.0)
    install(monkeypatch, adapter)

    assert cli_module.command_exec_smoke(parse(tmp_path, shares=10)) == 0

    open_leg, close_leg = (p.legs[0] for p in adapter.executed)
    assert open_leg.quantity == 10.0
    assert close_leg.quantity == 3.0


# --- the failure exits, which are the point ----------------------------------


def test_an_unknown_open_stops_and_prints_the_remedy(
    tmp_path, monkeypatch, capsys
) -> None:
    """`unknown` means an order may or may not be live. Closing a position that
    might not exist would open the opposite one."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(
        positions=(0.0, 5.0), open_status=ExecutionOutcomeStatus.UNKNOWN
    )
    install(monkeypatch, adapter)

    rc = cli_module.command_exec_smoke(parse(tmp_path, shares=5))
    out = capsys.readouterr().out

    assert rc == 1
    assert len(adapter.executed) == 1  # no close attempted
    assert "CRITICAL manual intervention required" in out
    assert "manual-close --venue ibkr" in out
    assert "--side sell --shares 5" in out


def test_a_failed_close_leaves_the_position_and_says_how_to_flatten(
    tmp_path, monkeypatch, capsys
) -> None:
    """The worst outcome of this tool: it opened a real position and could not
    close it."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(
        positions=(0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0),
        close_status=ExecutionOutcomeStatus.FAILED,
    )
    install(monkeypatch, adapter)

    rc = cli_module.command_exec_smoke(parse(tmp_path))
    out = capsys.readouterr().out

    assert rc == 1
    assert "CRITICAL manual intervention required" in out
    assert "manual-close --venue ibkr" in out
    assert "--side sell --shares 1" in out


def test_a_short_left_open_is_flattened_by_buying(
    tmp_path, monkeypatch, capsys
) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(
        positions=(0.0, -1.0, -1.0, -1.0, -1.0, -1.0, -1.0),
        close_status=ExecutionOutcomeStatus.FAILED,
    )
    install(monkeypatch, adapter)

    assert cli_module.command_exec_smoke(parse(tmp_path, side="sell")) == 1
    assert "--side buy --shares 1" in capsys.readouterr().out


def test_a_flat_reading_after_failure_is_not_treated_as_safe(
    tmp_path, monkeypatch, capsys
) -> None:
    """An unreadable position is not a flat one -- Phase D invariant 1."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(
        positions=(0.0, 1.0, 0.0), close_status=ExecutionOutcomeStatus.UNKNOWN
    )
    install(monkeypatch, adapter)

    rc = cli_module.command_exec_smoke(parse(tmp_path))
    out = capsys.readouterr().out

    assert rc == 1
    assert "verify against IBKR directly" in out
