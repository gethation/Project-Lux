from __future__ import annotations

import traceback
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

from ...core.models import StrategyState
from ...execution import ExecutionOutcome, ExecutionOutcomeStatus, ExecutionPreflight
from ...execution.intent import PairExecutionPlan
from ...reconciliation import BrokerAccountSnapshot
from ..subprocess_transport import SubprocessTransport
from .execution import FubonFutureExecutionAdapter, fubon_attempt_id
from .readonly import FubonReadOnlyBroker


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 30.0
DEFAULT_QUERY_TIMEOUT_SECONDS = 15.0
DEFAULT_TERMINATE_TIMEOUT_SECONDS = 3.0


class FubonExecutionWorkerError(RuntimeError):
    pass


class FubonExecutionWorkerTimeout(TimeoutError):
    pass


def _fubon_execution_worker(
    connection: Connection,
    symbol: str,
    env_path: Path | None,
) -> None:
    adapter = FubonFutureExecutionAdapter(symbol, env_path)
    readonly = FubonReadOnlyBroker(env_path, symbol=symbol)
    try:
        while True:
            request = connection.recv()
            operation = str(request["operation"])
            args = tuple(request.get("args", ()))
            should_stop = operation == "close"
            try:
                if operation in {"fetch_snapshot", "fetch_margins"}:
                    sdk, account = adapter._ensure_connected()
                    readonly.sdk = sdk
                    readonly.accounts = adapter.accounts
                    readonly.account = account
                    result = getattr(readonly, operation)(*args)
                else:
                    result = getattr(adapter, operation)(*args)
                response = {"ok": True, "result": result}
            except BaseException as exc:
                response = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            connection.send(response)
            if should_stop:
                return
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        try:
            adapter.close()
        except BaseException:
            pass
        connection.close()


class FubonFutureExecutionProcess:
    """OS-isolated facade for every stateful Fubon execution SDK call."""

    broker = FubonFutureExecutionAdapter.broker

    def __init__(
        self,
        symbol: str,
        env_path: Path | None = None,
        *,
        execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        query_timeout_seconds: float = DEFAULT_QUERY_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        worker_target: Callable[..., None] = _fubon_execution_worker,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.symbol = str(symbol)
        self.env_path = env_path
        self.execution_timeout_seconds = float(execution_timeout_seconds)
        self.query_timeout_seconds = float(query_timeout_seconds)
        self.terminate_timeout_seconds = float(terminate_timeout_seconds)
        self.worker_target = worker_target
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._transport = SubprocessTransport(
            worker_target=self.worker_target,
            worker_args=(self.symbol, self.env_path),
            process_name="project-lux-fubon-execution",
            broker_label="Fubon",
            worker_label="Fubon execution",
            closed_message="Fubon execution process is closed",
            error_type=FubonExecutionWorkerError,
            timeout_type=FubonExecutionWorkerTimeout,
            terminate_timeout_seconds=self.terminate_timeout_seconds,
        )

    @property
    def worker_pid(self) -> int | None:
        return self._transport.worker_pid

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        with self._transport.lock:
            try:
                result = self._request(
                    "execute",
                    plan,
                    timeout=self.execution_timeout_seconds,
                )
            except (FubonExecutionWorkerTimeout, FubonExecutionWorkerError) as exc:
                self._terminate_worker()
                return ExecutionOutcome(
                    plan_id=plan.plan_id,
                    timestamp=self.clock(),
                    status=ExecutionOutcomeStatus.UNKNOWN,
                    message=f"Fubon execution outcome unknown: {type(exc).__name__}: {exc}",
                    recommended_state=StrategyState.PAUSED,
                    payload={
                        "adapter": "fubon_execution_process",
                        "attempt_id": fubon_attempt_id(plan),
                        "confirmation_source": None,
                        "worker_error": type(exc).__name__,
                        "worker_error_message": str(exc),
                        "do_not_retry": True,
                    },
                )
        if not isinstance(result, ExecutionOutcome):
            raise FubonExecutionWorkerError(
                f"unexpected execution result: {type(result).__name__}"
            )
        return result

    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._query("fetch_open_orders"))

    def fetch_order_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._query("fetch_order_records"))

    def fetch_position_quantity(self) -> float:
        return float(self._query("fetch_position_quantity"))

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        result = self._readonly_query("fetch_snapshot")
        if not isinstance(result, BrokerAccountSnapshot):
            raise FubonExecutionWorkerError(
                f"unexpected snapshot type: {type(result).__name__}"
            )
        return result

    def fetch_margins(self) -> BrokerAccountSnapshot:
        result = self._readonly_query("fetch_margins")
        if not isinstance(result, BrokerAccountSnapshot):
            raise FubonExecutionWorkerError(
                f"unexpected margin snapshot type: {type(result).__name__}"
            )
        return result

    def preflight(self) -> ExecutionPreflight:
        result = self._query("preflight")
        if not isinstance(result, ExecutionPreflight):
            raise FubonExecutionWorkerError(
                f"unexpected preflight type: {type(result).__name__}"
            )
        return result

    def session_health(self) -> dict[str, Any]:
        result = self._query("session_health")
        health = dict(result)
        health["worker_pid"] = self.worker_pid
        return health

    def restart_worker(self) -> None:
        self._transport.restart(require_open=False)

    def close(self) -> None:
        self._transport.close(
            operation="close",
            payload={"operation": "close", "args": ()},
            timeout=self.terminate_timeout_seconds,
        )

    def _query(self, operation: str) -> Any:
        with self._transport.lock:
            try:
                return self._request(
                    operation,
                    timeout=self.query_timeout_seconds,
                )
            except (FubonExecutionWorkerTimeout, FubonExecutionWorkerError):
                self._terminate_worker()
                raise

    def _readonly_query(self, operation: str) -> Any:
        last_error: Exception | None = None
        for _ in range(2):
            try:
                return self._query(operation)
            except (FubonExecutionWorkerTimeout, FubonExecutionWorkerError) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _request(
        self,
        operation: str,
        *args: Any,
        timeout: float,
    ) -> Any:
        return self._transport.request(
            operation,
            payload={"operation": operation, "args": args},
            timeout=timeout,
        )

    def _terminate_worker(self) -> None:
        self._transport.terminate()

    def __enter__(self) -> "FubonFutureExecutionProcess":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "FubonExecutionWorkerError",
    "FubonExecutionWorkerTimeout",
    "FubonFutureExecutionProcess",
]
