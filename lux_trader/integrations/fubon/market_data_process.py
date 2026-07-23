from __future__ import annotations

import multiprocessing
import threading
import traceback
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ...market_data.types import LiveQuote
from .market_data import FubonTwLegMarketData


DEFAULT_INIT_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_TERMINATE_TIMEOUT_SECONDS = 3.0


class FubonMarketDataWorkerError(RuntimeError):
    """The isolated Fubon market-data worker failed a requested operation."""


class FubonMarketDataWorkerTimeout(TimeoutError):
    """The isolated Fubon market-data worker exceeded a hard deadline."""


def _fubon_market_data_worker(
    connection: Connection,
    env_path: Path | None,
    book_wait_timeout_seconds: float,
) -> None:
    provider = FubonTwLegMarketData(
        env_path,
        book_wait_timeout_seconds=book_wait_timeout_seconds,
    )
    try:
        while True:
            request = connection.recv()
            operation = str(request["operation"])
            args = tuple(request.get("args", ()))
            kwargs = dict(request.get("kwargs", {}))
            should_stop = operation == "close"
            try:
                method = getattr(provider, operation)
                result = method(*args, **kwargs)
                response = {
                    "ok": True,
                    "result": result,
                    "candidate_session_counts": provider.last_candidate_session_counts,
                    "candidate_session_summaries": (
                        provider.last_candidate_session_summaries
                    ),
                }
            except BaseException as exc:
                response = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "candidate_session_counts": provider.last_candidate_session_counts,
                    "candidate_session_summaries": (
                        provider.last_candidate_session_summaries
                    ),
                }
            connection.send(response)
            if should_stop:
                return
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        try:
            provider.close()
        except BaseException:
            pass
        connection.close()


class FubonTwLegMarketDataProcess:
    """Process-isolated facade for Fubon QFF market data.

    The Fubon SDK and all of its native threads and sockets live exclusively in
    the child process.  A stuck ``init_realtime`` or ``reconnect`` therefore has
    an OS-enforceable deadline: the parent terminates the worker and starts one
    clean replacement instead of blocking the live loop forever.
    """

    def __init__(
        self,
        env_path: Path | None = None,
        *,
        book_wait_timeout_seconds: float = 5.0,
        init_timeout_seconds: float = DEFAULT_INIT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        worker_target: Callable[..., None] = _fubon_market_data_worker,
    ) -> None:
        self.env_path = env_path
        self.book_wait_timeout_seconds = float(book_wait_timeout_seconds)
        self.init_timeout_seconds = float(init_timeout_seconds)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.terminate_timeout_seconds = float(terminate_timeout_seconds)
        if self.init_timeout_seconds <= 0:
            raise ValueError("init_timeout_seconds must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.terminate_timeout_seconds < 0:
            raise ValueError("terminate_timeout_seconds must not be negative")
        self._worker_target = worker_target
        self._context = multiprocessing.get_context("spawn")
        self._connection: Connection | None = None
        self._process: multiprocessing.Process | None = None
        self._connected = False
        self._closed = False
        self._lock = threading.RLock()
        self.last_candidate_session_counts: dict[str, int] = {}
        self.last_candidate_session_summaries: dict[str, str] = {}

    @property
    def worker_pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    def connect(self) -> None:
        with self._lock:
            self._require_open()
            if self._connected and self._worker_is_alive():
                return
            self._connect_with_one_rebuild("init_realtime")

    def reconnect(self) -> None:
        with self._lock:
            self._require_open()
            self.connect()
            try:
                self._request(
                    "reconnect",
                    timeout=self.init_timeout_seconds,
                    deadline_name="reconnect",
                )
                self._connected = True
            except FubonMarketDataWorkerTimeout:
                self._terminate_worker()
                self._connect_with_one_rebuild("reconnect rebuild")

    def fetch_candidates(self, product: str) -> list[Any]:
        return list(self._call("fetch_candidates", product))

    def select_front_month_symbol(self, product: str) -> str:
        return str(self._call("select_front_month_symbol", product))

    def fetch_quote(self, symbol: str) -> LiveQuote:
        result = self._call("fetch_quote", symbol)
        if not isinstance(result, LiveQuote):
            raise FubonMarketDataWorkerError(
                f"Fubon worker returned unexpected quote type: {type(result).__name__}"
            )
        return result

    def ensure_books_subscription(
        self,
        symbol: str,
        *,
        after_hours: bool | None = None,
    ) -> None:
        self._call(
            "ensure_books_subscription",
            symbol,
            after_hours=after_hours,
        )

    def unsubscribe_books(self, symbol: str) -> None:
        self._call("unsubscribe_books", symbol)

    def restart_books_session(
        self,
        symbol: str,
        *,
        after_hours: bool | None = None,
    ) -> None:
        self._call("restart_books_session", symbol, after_hours=after_hours)

    def teardown_books_session(self) -> None:
        self._call("teardown_books_session")

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        result = self._call("fetch_1m", symbol, start, end)
        if not isinstance(result, pd.DataFrame):
            raise FubonMarketDataWorkerError(
                f"Fubon worker returned unexpected candles type: {type(result).__name__}"
            )
        return result

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
                        deadline_name="close",
                    )
                except Exception:
                    pass
            self._terminate_worker()

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            self._require_open()
            self.connect()
            return self._request(
                operation,
                args=args,
                kwargs=kwargs,
                timeout=self.request_timeout_seconds,
                deadline_name=operation,
            )

    def _connect_with_one_rebuild(self, deadline_name: str) -> None:
        last_timeout: FubonMarketDataWorkerTimeout | None = None
        for attempt in range(2):
            self._start_worker()
            try:
                self._request(
                    "connect",
                    timeout=self.init_timeout_seconds,
                    deadline_name=deadline_name,
                )
                self._connected = True
                return
            except FubonMarketDataWorkerTimeout as exc:
                last_timeout = exc
                self._terminate_worker()
                if attempt == 0:
                    continue
                raise FubonMarketDataWorkerTimeout(
                    f"Fubon {deadline_name} timed out after "
                    f"{self.init_timeout_seconds:.1f}s in the replacement worker"
                ) from exc
            except Exception:
                self._terminate_worker()
                raise
        if last_timeout is not None:  # pragma: no cover - defensive exhaustiveness
            raise last_timeout

    def _start_worker(self) -> None:
        if self._worker_is_alive():
            return
        self._terminate_worker()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=self._worker_target,
            args=(child, self.env_path, self.book_wait_timeout_seconds),
            name="project-lux-fubon-market-data",
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._process = process
        self._connected = False

    def _request(
        self,
        operation: str,
        *,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        timeout: float,
        deadline_name: str,
    ) -> Any:
        connection = self._connection
        process = self._process
        if connection is None or process is None or not process.is_alive():
            raise FubonMarketDataWorkerError(
                f"Fubon worker is not running for {operation}"
            )
        try:
            connection.send(
                {
                    "operation": operation,
                    "args": args,
                    "kwargs": kwargs or {},
                }
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FubonMarketDataWorkerError(
                f"Fubon worker pipe failed during {operation}: {exc}"
            ) from exc
        if not connection.poll(max(float(timeout), 0.0)):
            raise FubonMarketDataWorkerTimeout(
                f"Fubon {deadline_name} exceeded hard timeout of {timeout:.1f}s"
            )
        try:
            response = connection.recv()
        except (BrokenPipeError, EOFError, OSError) as exc:
            raise FubonMarketDataWorkerError(
                f"Fubon worker exited during {operation}: {exc}"
            ) from exc
        self.last_candidate_session_counts = dict(
            response.get("candidate_session_counts", {})
        )
        self.last_candidate_session_summaries = dict(
            response.get("candidate_session_summaries", {})
        )
        if response.get("ok"):
            return response.get("result")
        error_type = str(response.get("error_type", "RuntimeError"))
        error = str(response.get("error", "unknown worker error"))
        detail = str(response.get("traceback", "")).strip()
        message = f"Fubon worker {operation} failed: {error_type}: {error}"
        if detail:
            message = f"{message}\n{detail}"
        raise FubonMarketDataWorkerError(message)

    def _worker_is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _terminate_worker(self) -> None:
        process = self._process
        connection = self._connection
        self._process = None
        self._connection = None
        self._connected = False
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
            raise RuntimeError("Fubon market-data process is closed")

    def __enter__(self) -> "FubonTwLegMarketDataProcess":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "DEFAULT_INIT_TIMEOUT_SECONDS",
    "FubonMarketDataWorkerError",
    "FubonMarketDataWorkerTimeout",
    "FubonTwLegMarketDataProcess",
]
