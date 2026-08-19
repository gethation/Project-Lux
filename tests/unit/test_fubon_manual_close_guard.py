"""Close-only guards on the Fubon half of `admin manual-close`.

The IBKR half has refused a non-closing side, an oversized close, and a close
against a flat account since it was written. The Fubon half printed a warning
and sent the order anyway, which made the recovery tool reproduce the incident
it exists for: on 2026-08-19 a `--side buy --lot 1` meant to restore a hedge
would have gone out as FutOptOrderType.Close against a position already at
zero and been rejected with 8481301, exactly as the automatic emergency close
had been minutes earlier.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import lux_trader.cli.commands_execution as cli_module
from lux_trader.cli.parser import build_parser
from lux_trader.execution.outcome import ExecutionOutcome, ExecutionOutcomeStatus

from test_ibkr_manual_close import SYMBOL as UMC_SYMBOL, write_config


CCF_SYMBOL = "CCFI6"


class FakeFubonAdapter:
    def __init__(self, *, position: float, open_orders: tuple = ()) -> None:
        self.position = position
        self.open_orders = list(open_orders)
        self.executed: list = []
        self.closed = False

    def fetch_position_quantity(self) -> float:
        return self.position

    def fetch_open_orders(self):
        return list(self.open_orders)

    def fetch_order_records(self):
        return []

    def execute(self, plan) -> ExecutionOutcome:
        self.executed.append(plan)
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=ExecutionOutcomeStatus.FILLED,
            message="fake",
            fills=(SimpleNamespace(quantity=plan.legs[0].quantity),),
        )

    def close(self) -> None:
        self.closed = True


def install(monkeypatch, adapter: FakeFubonAdapter) -> None:
    monkeypatch.setattr(
        cli_module, "FubonFutureExecutionAdapter", lambda *a, **k: adapter
    )
    monkeypatch.setattr(cli_module.time, "sleep", lambda *_: None)


def gates_open(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_FUBON_MANUAL_CLOSE", "1")


def parse(tmp_path: Path, **overrides) -> object:
    config = overrides.pop("config", None) or write_config(tmp_path)
    argv = [
        "admin", "manual-close", "--venue", "fubon",
        "--config", str(config),
        "--symbol", overrides.pop("symbol", CCF_SYMBOL),
        "--side", overrides.pop("side", "sell"),
        "--lot", str(overrides.pop("lot", 1)),
        "--confirm-symbol", overrides.pop("confirm_symbol", CCF_SYMBOL),
    ]
    if overrides.pop("allow_mismatch", False):
        argv.append("--allow-position-mismatch")
    return build_parser().parse_args(argv)


def test_refuses_to_re_open_a_hedge_against_a_flat_position(
    tmp_path, monkeypatch
) -> None:
    """The 2026-08-19 shape exactly: CCF already sold to zero, operator tries to
    buy one back to restore the hedge. This tool cannot do that, and saying so
    is better than sending an order the exchange will reject."""
    gates_open(monkeypatch)
    adapter = FakeFubonAdapter(position=0.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="nothing for --side buy to close"):
        cli_module.command_manual_close(parse(tmp_path, side="buy", lot=1))

    assert adapter.executed == []


def test_refuses_a_side_that_would_grow_the_position(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    adapter = FakeFubonAdapter(position=2.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="does not close a position"):
        cli_module.command_manual_close(parse(tmp_path, side="buy", lot=1))

    assert adapter.executed == []


def test_refuses_to_close_more_lots_than_are_held(tmp_path, monkeypatch) -> None:
    """Selling 3 against a long of 2 fills and leaves a naked short of 1."""
    gates_open(monkeypatch)
    adapter = FakeFubonAdapter(position=2.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="exceeds the position"):
        cli_module.command_manual_close(parse(tmp_path, side="sell", lot=3))

    assert adapter.executed == []


def test_a_genuine_close_still_goes_through(tmp_path, monkeypatch) -> None:
    """The guard must not break the job the tool exists for."""
    gates_open(monkeypatch)
    adapter = FakeFubonAdapter(position=2.0)
    install(monkeypatch, adapter)

    cli_module.command_manual_close(parse(tmp_path, side="sell", lot=2))

    assert len(adapter.executed) == 1
    assert adapter.executed[0].legs[0].quantity == 2


def test_the_override_exists_for_fubon_too(tmp_path, monkeypatch, capsys) -> None:
    """The broker's reported position is the thing an operator may believe is
    wrong. Blocking --allow-position-mismatch for Fubon while Fubon had no
    check at all was guarding an escape hatch for an open door."""
    gates_open(monkeypatch)
    adapter = FakeFubonAdapter(position=0.0)
    install(monkeypatch, adapter)

    cli_module.command_manual_close(
        parse(tmp_path, side="buy", lot=1, allow_mismatch=True)
    )

    assert len(adapter.executed) == 1
    assert "overriding close-only check" in capsys.readouterr().out
