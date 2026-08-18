from __future__ import annotations

import time
from multiprocessing.connection import Connection

from lux_trader.execution import ExecutionOutcomeStatus
from lux_trader.integrations.fubon.execution_process import (
    FubonFutureExecutionProcess,
)
from lux_trader.integrations.fubon.readonly_process import (
    FubonReadOnlyBrokerProcess,
    FubonReadOnlyWorkerTimeout,
)

from test_fubon_execution import SYMBOL, execution_plan, ts


def _hanging_execution_worker(
    connection: Connection,
    _symbol: str,
    _env_path,
) -> None:
    try:
        while True:
            connection.recv()
            time.sleep(30.0)
    except (EOFError, BrokenPipeError, OSError):
        return


def _hanging_readonly_worker(
    connection: Connection,
    _env_path,
    _symbol,
) -> None:
    try:
        while True:
            connection.recv()
            time.sleep(30.0)
    except (EOFError, BrokenPipeError, OSError):
        return


def test_execution_timeout_returns_unknown_and_kills_worker() -> None:
    adapter = FubonFutureExecutionProcess(
        SYMBOL,
        execution_timeout_seconds=1.0,
        terminate_timeout_seconds=0.2,
        worker_target=_hanging_execution_worker,
        clock=ts,
    )
    try:
        outcome = adapter.execute(execution_plan())

        assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
        assert outcome.recommended_state.value == "paused"
        assert outcome.payload["do_not_retry"] is True
        assert outcome.payload["attempt_id"] == "LUX-FUBON-PLAN-entry"
        assert adapter.worker_pid is None
    finally:
        adapter.close()


def test_readonly_timeout_kills_worker() -> None:
    broker = FubonReadOnlyBrokerProcess(
        symbol=SYMBOL,
        timeout_seconds=1.0,
        terminate_timeout_seconds=0.2,
        worker_target=_hanging_readonly_worker,
    )
    try:
        try:
            broker.fetch_snapshot()
        except FubonReadOnlyWorkerTimeout:
            pass
        else:  # pragma: no cover - assertion clarity
            raise AssertionError("expected readonly timeout")
        assert broker.worker_pid is None
    finally:
        broker.close()


def _symbol_recording_worker(
    connection: Connection,
    symbol: str,
    marker,
) -> None:
    """Records the symbol it was spawned with, then answers queries with 0.

    The child is a separate process, so a marker file is the only way to see
    which contract it actually received -- which is the whole point here.
    """
    with open(marker, "a", encoding="utf-8") as handle:
        handle.write(f"{symbol}" + chr(10))
        handle.flush()
    try:
        while True:
            connection.recv()
            connection.send({"ok": True, "result": 0.0})
    except (EOFError, BrokenPipeError, OSError):
        return


def _spawned_symbols(marker) -> list[str]:
    return [line for line in marker.read_text(encoding="utf-8").splitlines() if line]


def test_retargeting_a_rollover_moves_the_worker_to_the_new_contract(tmp_path) -> None:
    """REGRESSION 2026-08-18: the rollover moved the strategy and the market-data
    subscription to CCFI6 and left the order path on CCFH6. The first entry after
    it was rejected -- "Fubon leg symbol CCFI6 does not match CCFH6" -- and the
    strategy paused with no order ever submitted.

    The worker is scrapped rather than told the new symbol, because the child
    derives a contract identity and its own read-only broker from the symbol it
    was spawned with.
    """
    marker = tmp_path / "spawned-symbols.txt"
    marker.write_text("", encoding="utf-8")
    adapter = FubonFutureExecutionProcess(
        SYMBOL,
        marker,
        execution_timeout_seconds=1.0,
        terminate_timeout_seconds=0.2,
        worker_target=_symbol_recording_worker,
        clock=ts,
    )
    try:
        adapter.fetch_position_quantity()
        first_pid = adapter.worker_pid
        assert first_pid is not None
        assert _spawned_symbols(marker) == [SYMBOL]

        assert adapter.retarget_symbol("CCFI6") is True
        assert adapter.symbol == "CCFI6"
        # Scrapped immediately, so nothing built from the old contract survives.
        assert adapter.worker_pid is None

        adapter.fetch_position_quantity()
        assert adapter.worker_pid not in (None, first_pid)
        # The replacement child actually holds the new contract.
        assert _spawned_symbols(marker) == [SYMBOL, "CCFI6"]
    finally:
        adapter.close()


def test_retargeting_to_the_same_contract_does_not_disturb_the_worker(tmp_path) -> None:
    """A rollover check runs every bar. Rebuilding the SDK session each time
    would be a real cost for no reason, so an unchanged symbol is a no-op."""
    marker = tmp_path / "spawned-symbols.txt"
    marker.write_text("", encoding="utf-8")
    adapter = FubonFutureExecutionProcess(
        SYMBOL,
        marker,
        execution_timeout_seconds=1.0,
        terminate_timeout_seconds=0.2,
        worker_target=_symbol_recording_worker,
        clock=ts,
    )
    try:
        adapter.fetch_position_quantity()
        pid = adapter.worker_pid

        assert adapter.retarget_symbol(SYMBOL) is False
        assert adapter.retarget_symbol("  ") is False
        assert adapter.worker_pid == pid
        assert _spawned_symbols(marker) == [SYMBOL]
    finally:
        adapter.close()
