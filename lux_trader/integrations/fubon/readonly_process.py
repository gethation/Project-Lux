from __future__ import annotations

import traceback
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

from ...core.models import BrokerName
from ...reconciliation import BrokerAccountSnapshot
from ..subprocess_transport import SubprocessTransport
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
        self._transport = SubprocessTransport(
            worker_target=self.worker_target,
            worker_args=(self.env_path, self.symbol),
            process_name="project-lux-fubon-readonly",
            broker_label="Fubon",
            worker_label="Fubon readonly",
            closed_message="Fubon readonly process is closed",
            error_type=FubonReadOnlyWorkerError,
            timeout_type=FubonReadOnlyWorkerTimeout,
            terminate_timeout_seconds=self.terminate_timeout_seconds,
        )

    @property
    def worker_pid(self) -> int | None:
        return self._transport.worker_pid

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
        self._transport.close(
            operation="close",
            payload={"operation": "close"},
            timeout=self.terminate_timeout_seconds,
        )

    def restart_worker(self) -> None:
        """Discard a possibly stale SDK session; the next query starts clean."""

        self._transport.restart(require_open=True)

    def _request_guarded(self, operation: str) -> Any:
        with self._transport.lock:
            try:
                return self._request(operation, timeout=self.timeout_seconds)
            except (FubonReadOnlyWorkerTimeout, FubonReadOnlyWorkerError):
                self._terminate_worker()
                raise

    def _request(self, operation: str, *, timeout: float) -> Any:
        return self._transport.request(
            operation,
            payload={"operation": operation},
            timeout=timeout,
        )

    def _terminate_worker(self) -> None:
        self._transport.terminate()

    def __enter__(self) -> "FubonReadOnlyBrokerProcess":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


__all__ = [
    "FubonReadOnlyBrokerProcess",
    "FubonReadOnlyWorkerError",
    "FubonReadOnlyWorkerTimeout",
]
