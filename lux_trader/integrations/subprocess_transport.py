from __future__ import annotations

import multiprocessing
import threading
from multiprocessing.connection import Connection
from typing import Any, Callable, Mapping


class SubprocessTransport:
    """Reusable spawn-process request transport with caller-owned policy."""

    def __init__(
        self,
        *,
        worker_target: Callable[..., None],
        worker_args: tuple[Any, ...],
        process_name: str,
        broker_label: str,
        worker_label: str,
        closed_message: str,
        error_type: type[Exception],
        timeout_type: type[Exception],
        terminate_timeout_seconds: float,
    ) -> None:
        self.worker_target = worker_target
        self.worker_args = worker_args
        self.process_name = process_name
        self.broker_label = broker_label
        self.worker_label = worker_label
        self.closed_message = closed_message
        self.error_type = error_type
        self.timeout_type = timeout_type
        self.terminate_timeout_seconds = float(terminate_timeout_seconds)
        self._context = multiprocessing.get_context("spawn")
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._closed = False
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    @property
    def worker_is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def request(
        self,
        operation: str,
        *,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> Any:
        with self._lock:
            self._require_open()
            self._start_worker()
            connection = self._connection
            process = self._process
            if connection is None or process is None or not process.is_alive():
                raise self.error_type(f"{self.worker_label} worker is not running")
            try:
                connection.send(dict(payload))
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise self.error_type(
                    f"worker pipe failed during {operation}: {exc}"
                ) from exc
            if not connection.poll(max(float(timeout), 0.0)):
                raise self.timeout_type(
                    f"{self.broker_label} {operation} exceeded hard timeout of "
                    f"{timeout:.1f}s"
                )
            try:
                response = connection.recv()
            except (BrokenPipeError, EOFError, OSError) as exc:
                raise self.error_type(
                    f"worker exited during {operation}: {exc}"
                ) from exc
            if response.get("ok"):
                return response.get("result")
            message = (
                f"{self.broker_label} worker {operation} failed: "
                f"{response.get('error_type', 'RuntimeError')}: "
                f"{response.get('error', 'unknown error')}"
            )
            raise self.error_type(message)

    def terminate(self) -> None:
        with self._lock:
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

    def restart(self, *, require_open: bool) -> None:
        with self._lock:
            if require_open:
                self._require_open()
            self.terminate()

    def close(
        self,
        *,
        operation: str,
        payload: Mapping[str, Any],
        timeout: float,
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self.worker_is_alive:
                try:
                    self.request(
                        operation,
                        payload=payload,
                        timeout=timeout,
                    )
                except Exception:
                    pass
            self.terminate()

    def _start_worker(self) -> None:
        if self.worker_is_alive:
            return
        self.terminate()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self.worker_target,
            args=(child, *self.worker_args),
            name=self.process_name,
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._process = process

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError(self.closed_message)


__all__ = ["SubprocessTransport"]
