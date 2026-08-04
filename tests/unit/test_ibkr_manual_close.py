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

    # The real adapter exposes preflight(), fetch_position_quantity() and
    # fetch_open_orders(); the double must too, or it silently defines a
    # narrower contract than the code under test relies on.
    def _current_position(self) -> float:
        if self.executed and self.position_after is not None:
            return self.position_after
        return self.position

    def preflight(self):
        self._preflight_calls += 1
        return SimpleNamespace(
            position_quantity=self._current_position(),
            open_orders=() if self.executed else self.open_orders,
        )

    def fetch_position_quantity(self) -> float:
        return self._current_position()

    def fetch_open_orders(self):
        return () if self.executed else self.open_orders

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
    # The command now polls for the post-fill position; the real sleep would
    # add 2s per test for a lag the double does not simulate.
    monkeypatch.setattr(cli_module.time, "sleep", lambda *_: None)


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


def test_a_symbol_that_is_not_the_configured_umc_is_refused(
    tmp_path, monkeypatch
) -> None:
    """THE WORST BUG THIS FILE GUARDS.

    IbkrClientProcess takes no symbol -- it hardcodes Stock("UMC","SMART","USD")
    and filters positions on "UMC". So an unchecked --symbol is not ignored, it
    is a lie: `--symbol TSM --confirm-symbol TSM` used to pass (the two only
    matched each other), evaluate the close-only guard against the UMC position,
    and send a real UMC market order recorded in the journal as TSM.
    """
    gates_open(monkeypatch)
    adapter = FakeIbkrAdapter(position=389.0)
    install(monkeypatch, adapter)

    with pytest.raises(SystemExit, match="not the configured UMC symbol"):
        cli_module.command_manual_close(
            parse(tmp_path, symbol="TSM", confirm_symbol="TSM", shares=100)
        )

    assert adapter.executed == []


def test_the_post_close_read_is_polled_not_taken_once(
    tmp_path, monkeypatch, capsys
) -> None:
    """REGRESSION: a clean close reported CRITICAL.

    IBKR's portfolio view lags its own fill report, so the single read taken
    immediately after the order returned the PRE-close position, the expected
    check failed, and the tool exited 1 on a successful close. An operator
    acting on that false alarm reruns and opens a naked position -- the one
    outcome this tool exists to prevent.
    """
    gates_open(monkeypatch)

    class LaggingAdapter(FakeIbkrAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._reads = 0

        def fetch_position_quantity(self) -> float:
            self._reads += 1
            # Stale for the first two reads, then the truth.
            return 500.0 if self._reads <= 2 else 0.0

    adapter = LaggingAdapter(position=500.0)
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(
        parse(tmp_path, side="sell", shares=500)
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "CRITICAL" not in out
    assert adapter._reads > 1


def test_the_expectation_uses_the_filled_quantity_not_the_requested(
    tmp_path, monkeypatch, capsys
) -> None:
    """A partial fill must not be judged against the size that was asked for."""
    gates_open(monkeypatch)

    class PartialAdapter(FakeIbkrAdapter):
        def execute(self, plan):
            self.executed.append(plan)
            return ExecutionOutcome(
                plan_id=plan.plan_id,
                timestamp=plan.timestamp,
                status=ExecutionOutcomeStatus.FILLED,
                message="partial",
                fills=(SimpleNamespace(quantity=200.0),),
            )

    # 500 requested, 200 filled, so 300 should remain -- not an error.
    adapter = PartialAdapter(position=500.0, position_after=300.0)
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(parse(tmp_path, side="sell", shares=500))

    assert rc == 0
    assert "CRITICAL" not in capsys.readouterr().out


def test_an_unreadable_position_after_the_order_is_critical_not_a_traceback(
    tmp_path, monkeypatch, capsys
) -> None:
    """The read most likely to fail is the one taken right after a close that
    was needed because the venue is unhealthy."""
    gates_open(monkeypatch)

    class BlindAfterOrder(FakeIbkrAdapter):
        def fetch_position_quantity(self) -> float:
            if self.executed:
                raise RuntimeError("worker died")
            return self.position

    adapter = BlindAfterOrder(position=389.0)
    install(monkeypatch, adapter)

    rc = cli_module.command_manual_close(parse(tmp_path, side="sell", shares=389))
    out = capsys.readouterr().out

    assert rc == 1
    assert "POSITION UNREADABLE" in out
    assert "CRITICAL manual intervention required" in out


def test_fubon_manual_close_refuses_the_ibkr_only_flags(tmp_path, monkeypatch) -> None:
    """Widening --venue turned previously-rejected flags into accepted no-ops."""
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_FUBON_MANUAL_CLOSE", "1")
    args = build_parser().parse_args(
        [
            "admin", "manual-close", "--venue", "fubon",
            "--config", str(write_config(tmp_path)),
            "--symbol", "CCFH6", "--side", "sell", "--lot", "1",
            "--shares", "400",
            "--confirm-symbol", "CCFH6",
        ]
    )

    with pytest.raises(SystemExit, match="--shares"):
        cli_module.command_manual_close(args)


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
