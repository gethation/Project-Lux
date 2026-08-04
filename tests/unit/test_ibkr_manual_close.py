"""Emergency-closing a stranded UMC leg.

Until 2026-08-04 this did not exist: `manual-close --venue` accepted only
`fubon`, so the one state the tool is for -- the CCF leg filled and the UMC leg
did not -- had no remedy from this CLI.

The tests that matter here are the refusals. A close that is larger than the
position, or on the wrong side, does not fail loudly at the broker: it fills,
reports success, and leaves a naked position twice the size in the opposite
direction. That is worse than the problem the operator was trying to fix.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import lux_trader.cli.commands_execution as cli_module
from lux_trader.cli.commands_execution import side_closes_position
from lux_trader.cli.parser import build_parser
from lux_trader.core.models import OrderSide
from lux_trader.execution.outcome import ExecutionOutcome, ExecutionOutcomeStatus


SYMBOL = "UMC"


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
        position: float = 0.0,
        position_after: float | None = None,
        open_orders: tuple[dict, ...] = (),
        status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.FILLED,
    ) -> None:
        self.position = position
        self.position_after = position_after
        self.open_orders = open_orders
        self.status = status
        self.executed = []
        self.closed = False
        self._preflight_calls = 0

    def preflight(self):
        self._preflight_calls += 1
        if self._preflight_calls > 1 and self.position_after is not None:
            return SimpleNamespace(
                position_quantity=self.position_after, open_orders=()
            )
        return SimpleNamespace(
            position_quantity=self.position, open_orders=self.open_orders
        )

    def execute(self, plan):
        self.executed.append(plan)
        leg = plan.legs[0]
        fills = ()
        if self.status == ExecutionOutcomeStatus.FILLED:
            fills = (SimpleNamespace(quantity=leg.quantity),)
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=self.status,
            message="fake",
            fills=fills,
        )

    def close(self) -> None:
        self.closed = True


def install(monkeypatch, adapter: FakeIbkrAdapter) -> None:
    monkeypatch.setattr(
        cli_module, "IbkrUmcExecutionAdapter", lambda *a, **k: adapter
    )


def gates_open(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("IBKR_ALLOW_LIVE_ORDER", "1")


def parse(tmp_path: Path, **overrides) -> object:
    # Lazily, because write_config() writes to a fixed path: passing it as a
    # pop() default would evaluate eagerly and overwrite a config the caller
    # deliberately wrote with different settings.
    config = overrides.pop("config", None) or write_config(tmp_path)
    argv = [
        "admin", "manual-close", "--venue", "ibkr",
        "--config", str(config),
        "--symbol", overrides.pop("symbol", SYMBOL),
        "--side", overrides.pop("side", "sell"),
        "--shares", str(overrides.pop("shares", 100)),
        "--confirm-symbol", overrides.pop("confirm_symbol", SYMBOL),
    ]
    if overrides.pop("allow_mismatch", False):
        argv.append("--allow-position-mismatch")
    return build_parser().parse_args(argv)


# --- the guard that stops this tool causing the disaster it prevents ----------


def test_side_that_grows_the_position_is_not_a_close() -> None:
    assert side_closes_position(389.0, OrderSide.SELL) is True
    assert side_closes_position(-389.0, OrderSide.BUY) is True
    assert side_closes_position(389.0, OrderSide.BUY) is False
    assert side_closes_position(-389.0, OrderSide.SELL) is False
    # Nothing closes a flat position.
    assert side_closes_position(0.0, OrderSide.SELL) is False
    assert side_closes_position(0.0, OrderSide.BUY) is False


def test_refuses_to_close_more_than_the_position(tmp_path, monkeypatch) -> None:
    """The disaster case: selling 500 against a long of 389 does not error at
    the broker, it fills and leaves a naked short of 111."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="exceeds the position"):
        cli_module.command_manual_close(parse(tmp_path, side="sell", shares=500))

    assert adapter.executed == []
    assert adapter.closed is True


def test_refuses_a_side_that_would_increase_the_position(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="does not close a position"):
        cli_module.command_manual_close(parse(tmp_path, side="buy", shares=100))

    assert adapter.executed == []


def test_refuses_to_open_a_position_from_flat(tmp_path, monkeypatch) -> None:
    """A zero position means there is nothing to close. Sending the order
    anyway would open one."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=0.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="does not close a position"):
        cli_module.command_manual_close(parse(tmp_path, side="sell", shares=100))

    assert adapter.executed == []


def test_the_override_exists_and_is_explicit(tmp_path, monkeypatch, capsys) -> None:
    """The broker's reported position is the thing the operator may believe is
    wrong, so the guard is escapable -- but only by saying so."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=0.0, position_after=-100.0)
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(
        parse(tmp_path, side="sell", shares=100, allow_mismatch=True)
    )

    assert rc == 0
    assert len(adapter.executed) == 1
    assert "overriding close-only check" in capsys.readouterr().out


def test_refuses_when_open_orders_exist(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0, open_orders=({"order_id": 1},))
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="existing open orders"):
        cli_module.command_manual_close(parse(tmp_path, side="sell", shares=389))

    assert adapter.executed == []


# --- gates -------------------------------------------------------------------


def test_requires_both_env_gates(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PROJECT_LUX_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.delenv("IBKR_ALLOW_LIVE_ORDER", raising=False)
    install(monkeypatch, FakeIbkrAdapter(position=389.0))

    with pytest.raises(SystemExit, match="gates closed"):
        cli_module.command_manual_close(parse(tmp_path))


def test_requires_allow_live_order_in_config(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    install(monkeypatch, FakeIbkrAdapter(position=389.0))
    config = write_config(tmp_path, allow_live_order=False)

    with pytest.raises(SystemExit, match="allow_live_order"):
        cli_module.command_manual_close(parse(tmp_path, config=config))


def test_requires_confirm_symbol_to_match(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    install(monkeypatch, FakeIbkrAdapter(position=389.0))

    with pytest.raises(SystemExit, match="confirm-symbol"):
        cli_module.command_manual_close(parse(tmp_path, confirm_symbol="TSM"))


def test_requires_shares(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    install(monkeypatch, FakeIbkrAdapter(position=389.0))
    args = build_parser().parse_args(
        [
            "admin", "manual-close", "--venue", "ibkr",
            "--config", str(write_config(tmp_path)),
            "--symbol", SYMBOL, "--side", "sell",
            "--confirm-symbol", SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="--shares is required"):
        cli_module.command_manual_close(args)


# --- the happy path, and what it verifies afterwards --------------------------


def test_a_clean_close_verifies_the_position_moved(tmp_path, monkeypatch) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0, position_after=0.0)
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(parse(tmp_path, side="sell", shares=389))

    assert rc == 0
    plan = adapter.executed[0]
    leg = plan.legs[0]
    assert leg.side == OrderSide.SELL
    assert leg.quantity == 389.0
    assert leg.symbol == "UMC"
    assert plan.plan_type.value == "exit"
    assert adapter.closed is True


def test_a_reported_fill_that_did_not_move_the_position_is_critical(
    tmp_path, monkeypatch, capsys
) -> None:
    """Trusting the fill report over the position is how a naked leg survives a
    close that claimed to work."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0, position_after=389.0)
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(parse(tmp_path, side="sell", shares=389))

    assert rc == 1
    assert "CRITICAL position" in capsys.readouterr().out


def test_an_unfilled_outcome_is_critical(tmp_path, monkeypatch, capsys) -> None:
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(
        position=389.0, status=ExecutionOutcomeStatus.UNKNOWN
    )
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(parse(tmp_path, side="sell", shares=389))

    assert rc == 1
    assert "CRITICAL manual intervention required" in capsys.readouterr().out


def test_a_partial_close_is_allowed(tmp_path, monkeypatch) -> None:
    """Closing 100 of a 389 long is a reduction, not a flip, so it is fine."""
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0, position_after=289.0)
    install(monkeypatch, adapter)

    assert cli_module.command_manual_close(
        parse(tmp_path, side="sell", shares=100)
    ) == 0
    assert adapter.executed[0].legs[0].quantity == 100.0
