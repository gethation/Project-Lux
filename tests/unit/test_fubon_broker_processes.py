from __future__ import annotations

import inspect
import time
from multiprocessing.connection import Connection

from lux_trader.execution import ExecutionOutcomeStatus
from lux_trader.integrations.fubon.execution_process import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    DEFAULT_QUERY_TIMEOUT_SECONDS,
    DEFAULT_TERMINATE_TIMEOUT_SECONDS as DEFAULT_EXECUTION_TERMINATE_TIMEOUT_SECONDS,
    FubonFutureExecutionProcess,
)
from lux_trader.integrations.fubon.readonly_process import (
    DEFAULT_READONLY_TIMEOUT_SECONDS,
    DEFAULT_TERMINATE_TIMEOUT_SECONDS as DEFAULT_READONLY_TERMINATE_TIMEOUT_SECONDS,
    FubonReadOnlyBrokerProcess,
    FubonReadOnlyWorkerTimeout,
)
from lux_trader.integrations.subprocess_transport import SubprocessTransport

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


def test_subprocess_transport_preserves_adapter_timeouts_without_defaults() -> None:
    execution = FubonFutureExecutionProcess(SYMBOL)
    readonly = FubonReadOnlyBrokerProcess(symbol=SYMBOL)
    try:
        assert (
            execution.execution_timeout_seconds
            == DEFAULT_EXECUTION_TIMEOUT_SECONDS
            == 30.0
        )
        assert (
            execution.query_timeout_seconds
            == DEFAULT_QUERY_TIMEOUT_SECONDS
            == 15.0
        )
        assert (
            execution.terminate_timeout_seconds
            == DEFAULT_EXECUTION_TERMINATE_TIMEOUT_SECONDS
            == 3.0
        )
        assert readonly.timeout_seconds == DEFAULT_READONLY_TIMEOUT_SECONDS == 20.0
        assert (
            readonly.terminate_timeout_seconds
            == DEFAULT_READONLY_TERMINATE_TIMEOUT_SECONDS
            == 3.0
        )

        init_parameters = inspect.signature(SubprocessTransport.__init__).parameters
        request_parameters = inspect.signature(SubprocessTransport.request).parameters
        assert (
            init_parameters["terminate_timeout_seconds"].default
            is inspect.Parameter.empty
        )
        assert request_parameters["timeout"].default is inspect.Parameter.empty
    finally:
        execution.close()
        readonly.close()


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
