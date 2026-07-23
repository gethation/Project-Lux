from __future__ import annotations

import time
from datetime import datetime
from multiprocessing.connection import Connection

from lux_trader.core.models import BrokerName
from lux_trader.execution import ExecutionOutcomeStatus
from lux_trader.integrations.fubon.execution_process import (
    FubonFutureExecutionProcess,
)
from lux_trader.reconciliation import BrokerAccountSnapshot

from test_fubon_execution import SYMBOL, execution_plan, ts


def _hanging_worker(
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


def _snapshot_worker(
    connection: Connection,
    symbol: str,
    _env_path,
) -> None:
    generation = 1
    try:
        while True:
            request = connection.recv()
            operation = request["operation"]
            if operation == "fetch_snapshot":
                result = BrokerAccountSnapshot(
                    broker=BrokerName.FUBON,
                    account_id="test-fubon",
                    fetched_at=datetime(2026, 7, 22),
                    positions=(),
                    open_orders=(),
                    raw={"symbol": symbol},
                )
            elif operation == "session_health":
                result = {
                    "role": "trading",
                    "generation": generation,
                    "status": "ready",
                    "last_login_at": None,
                    "last_success_at": None,
                    "invalid_reason": None,
                    "relogin_count": 0,
                }
            elif operation == "close":
                connection.send({"ok": True, "result": None})
                return
            else:
                raise AssertionError(f"unexpected operation {operation}")
            connection.send({"ok": True, "result": result})
    except (EOFError, BrokenPipeError, OSError):
        return


def test_execution_timeout_is_unknown_and_never_retried() -> None:
    gateway = FubonFutureExecutionProcess(
        SYMBOL,
        execution_timeout_seconds=0.2,
        terminate_timeout_seconds=0.1,
        worker_target=_hanging_worker,
        clock=ts,
    )
    try:
        outcome = gateway.execute(execution_plan())

        assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
        assert outcome.payload["do_not_retry"] is True
        assert gateway.worker_pid is None
    finally:
        gateway.close()


def test_account_snapshot_and_health_share_one_worker() -> None:
    gateway = FubonFutureExecutionProcess(
        SYMBOL,
        worker_target=_snapshot_worker,
    )
    try:
        snapshot = gateway.fetch_snapshot()
        first_pid = gateway.worker_pid
        health = gateway.session_health()

        assert snapshot.broker == BrokerName.FUBON
        assert first_pid is not None
        assert health["worker_pid"] == first_pid
        assert health["generation"] == 1
    finally:
        gateway.close()
