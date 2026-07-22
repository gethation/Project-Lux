from __future__ import annotations

import multiprocessing
import threading
import traceback
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

from ...core.models import StrategyState
from ...execution import ExecutionOutcome, ExecutionOutcomeStatus
from ...execution.intent import PairExecutionPlan
from ...reconciliation import BrokerAccountSnapshot
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
        self._context = multiprocessing.get_context("spawn")
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        with self._lock:
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

    def preflight(self) -> Any:
        return self._query("preflight")

    def session_health(self) -> dict[str, Any]:
        result = self._query("session_health")
        health = dict(result)
        health["worker_pid"] = self.worker_pid
        return health

    def restart_worker(self) -> None:
        with self._lock:
            self._terminate_worker()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._worker_is_alive():
                try:
                    self._request(
                        "close",
                        timeout=self.terminate_timeout_seconds,
                    )
                except Exception:
                    pass
            self._terminate_worker()

    def _query(self, operation: str) -> Any:
        with self._lock:
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
        self._require_open()
        self._start_worker()
        connection = self._connection
        process = self._process
        if connection is None or process is None or not process.is_alive():
            raise FubonExecutionWorkerError("Fubon execution worker is not running")
        try:
            connection.send({"operation": operation, "args": args})
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FubonExecutionWorkerError(
                f"worker pipe failed during {operation}: {exc}"
            ) from exc
        if not connection.poll(max(float(timeout), 0.0)):
            raise FubonExecutionWorkerTimeout(
                f"Fubon {operation} exceeded hard timeout of {timeout:.1f}s"
            )
        try:
            response = connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FubonExecutionWorkerError(
                f"worker exited during {operation}: {exc}"
            ) from exc
        if response.get("ok"):
            return response.get("result")
        message = (
            f"Fubon worker {operation} failed: "
            f"{response.get('error_type', 'RuntimeError')}: "
            f"{response.get('error', 'unknown error')}"
        )
        raise FubonExecutionWorkerError(message)

    def _start_worker(self) -> None:
        if self._worker_is_alive():
            return
        self._terminate_worker()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self.worker_target,
            args=(child, self.symbol, self.env_path),
            name="project-lux-fubon-execution",
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._process = process

    def _worker_is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _terminate_worker(self) -> None:
        process = self._process
        connection = self._connection
        self._process = None
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(self.terminate_timeout_seconds)
        if process.is_alive():
            process.kill()
            process.join(self.terminate_timeout_seconds)
        else:
            process.join(timeout=0)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Fubon execution process is closed")

    def __enter__(self) -> "FubonFutureExecutionProcess":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "FubonExecutionWorkerError",
    "FubonExecutionWorkerTimeout",
    "FubonFutureExecutionProcess",
]
