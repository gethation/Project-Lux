from __future__ import annotations

import multiprocessing
import threading
import traceback
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

from ...core.models import BrokerName
from ...reconciliation import BrokerAccountSnapshot
from .readonly import FubonReadOnlyBroker


DEFAULT_READONLY_TIMEOUT_SECONDS = 20.0
DEFAULT_TERMINATE_TIMEOUT_SECONDS = 3.0


class FubonReadOnlyWorkerError(RuntimeError):
    pass


class FubonReadOnlyWorkerTimeout(TimeoutError):
    pass


def _fubon_readonly_worker(
    connection: Connection,
    env_path: Path | None,
    symbol: str | None,
) -> None:
    broker = FubonReadOnlyBroker(env_path, symbol=symbol)
    try:
        while True:
            request = connection.recv()
            operation = str(request["operation"])
            should_stop = operation == "close"
            try:
                result = getattr(broker, operation)()
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
            broker.close()
        except BaseException:
            pass
        connection.close()


class FubonReadOnlyBrokerProcess:
    """Hard-timeout process boundary for reconciliation/account queries."""

    broker = BrokerName.FUBON_QFF

    def __init__(
        self,
        env_path: Path | None = None,
        *,
        symbol: str | None = None,
        timeout_seconds: float = DEFAULT_READONLY_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        worker_target: Callable[..., None] = _fubon_readonly_worker,
    ) -> None:
        self.env_path = env_path
        self.symbol = symbol
        self.timeout_seconds = float(timeout_seconds)
        self.terminate_timeout_seconds = float(terminate_timeout_seconds)
        self.worker_target = worker_target
        self._context = multiprocessing.get_context("spawn")
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        result = self._request_guarded("fetch_snapshot")
        if not isinstance(result, BrokerAccountSnapshot):
            raise FubonReadOnlyWorkerError(
                f"unexpected snapshot type: {type(result).__name__}"
            )
        return result

    def fetch_margins(self) -> BrokerAccountSnapshot:
        result = self._request_guarded("fetch_margins")
        if not isinstance(result, BrokerAccountSnapshot):
            raise FubonReadOnlyWorkerError(
                f"unexpected margin snapshot type: {type(result).__name__}"
            )
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._worker_is_alive():
                try:
                    self._request("close", timeout=self.terminate_timeout_seconds)
                except Exception:
                    pass
            self._terminate_worker()

    def restart_worker(self) -> None:
        """Discard a possibly stale SDK session; the next query starts clean."""

        with self._lock:
            if self._closed:
                raise RuntimeError("Fubon readonly process is closed")
            self._terminate_worker()

    def _request_guarded(self, operation: str) -> Any:
        with self._lock:
            try:
                return self._request(operation, timeout=self.timeout_seconds)
            except (FubonReadOnlyWorkerTimeout, FubonReadOnlyWorkerError):
                self._terminate_worker()
                raise

    def _request(self, operation: str, *, timeout: float) -> Any:
        if self._closed:
            raise RuntimeError("Fubon readonly process is closed")
        self._start_worker()
        connection = self._connection
        process = self._process
        if connection is None or process is None or not process.is_alive():
            raise FubonReadOnlyWorkerError("Fubon readonly worker is not running")
        try:
            connection.send({"operation": operation})
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FubonReadOnlyWorkerError(
                f"worker pipe failed during {operation}: {exc}"
            ) from exc
        if not connection.poll(max(float(timeout), 0.0)):
            raise FubonReadOnlyWorkerTimeout(
                f"Fubon {operation} exceeded hard timeout of {timeout:.1f}s"
            )
        try:
            response = connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FubonReadOnlyWorkerError(
                f"worker exited during {operation}: {exc}"
            ) from exc
        if response.get("ok"):
            return response.get("result")
        raise FubonReadOnlyWorkerError(
            f"Fubon worker {operation} failed: "
            f"{response.get('error_type', 'RuntimeError')}: "
            f"{response.get('error', 'unknown error')}"
        )

    def _start_worker(self) -> None:
        if self._worker_is_alive():
            return
        self._terminate_worker()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self.worker_target,
            args=(child, self.env_path, self.symbol),
            name="project-lux-fubon-readonly",
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

    def __enter__(self) -> "FubonReadOnlyBrokerProcess":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "FubonReadOnlyBrokerProcess",
    "FubonReadOnlyWorkerError",
    "FubonReadOnlyWorkerTimeout",
]
