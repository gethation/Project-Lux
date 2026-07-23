from __future__ import annotations

import traceback
import math
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Connection
from typing import Any, Callable

from ib_async import IB, Stock, StartupFetch

from ...core.time import TAIPEI_TZ
from ..subprocess_transport import SubprocessTransport


DEFAULT_CLIENT_ID = 17_002
DEFAULT_CONNECT_TIMEOUT_SECONDS = 8.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_TERMINATE_TIMEOUT_SECONDS = 3.0


class IbkrWorkerError(RuntimeError):
    """The isolated IBKR worker failed a requested read-only operation."""


class IbkrWorkerTimeout(TimeoutError):
    """The isolated IBKR worker exceeded a hard parent-side deadline."""


class IbkrGatewayUnavailable(RuntimeError):
    """IB Gateway is at its daily login screen or otherwise not listening."""


@dataclass(frozen=True)
class IbkrConnectionConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = DEFAULT_CLIENT_ID
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if self.port <= 0:
            raise ValueError("port must be positive")
        if self.client_id < 0:
            raise ValueError("client_id must not be negative")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")


@dataclass(frozen=True)
class IbkrContractDetails:
    con_id: int
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    long_name: str
    time_zone_id: str
    trading_hours: str
    liquid_hours: str


class _IbkrWorkerClient:
    """Stateful ib_async owner. Instances exist only in the child process."""

    def __init__(
        self,
        config: IbkrConnectionConfig,
        *,
        ib_factory: Callable[[], Any] = IB,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.ib = ib_factory()
        self.clock = clock or (lambda: datetime.now(tz=TAIPEI_TZ))
        self._status = "not_connected"
        self._last_error_code: int | None = None
        self._last_error_message: str | None = None
        self._last_event_at: datetime | None = None
        self._data_lost = False
        self._umc_detail: Any | None = None
        self._register_events()

    def _register_events(self) -> None:
        self.ib.errorEvent += self._on_error
        self.ib.connectedEvent += self._on_connected
        self.ib.disconnectedEvent += self._on_disconnected

    def _stamp(
        self,
        status: str,
        *,
        error_code: int | None = None,
        message: str | None = None,
        data_lost: bool | None = None,
    ) -> None:
        self._status = status
        self._last_error_code = error_code
        self._last_error_message = message
        if data_lost is not None:
            self._data_lost = data_lost
        self._last_event_at = self.clock()

    def _on_connected(self, *_args: object) -> None:
        self._stamp("connected", data_lost=False)

    def _on_disconnected(self, *_args: object) -> None:
        self._stamp(
            "gateway_unavailable",
            message="IB Gateway socket disconnected; login may be required",
        )

    def _on_error(
        self,
        _request_id: int,
        error_code: int,
        error_message: str,
        _contract: object,
    ) -> None:
        code = int(error_code)
        message = str(error_message)
        if code == 1100:
            self._stamp(
                "connectivity_lost",
                error_code=code,
                message=message,
                data_lost=True,
            )
        elif code == 1101:
            self._stamp(
                "restored_data_lost",
                error_code=code,
                message=message,
                data_lost=True,
            )
        elif code == 1102:
            self._stamp(
                "restored",
                error_code=code,
                message=message,
                data_lost=False,
            )

    def connect(self) -> dict[str, Any]:
        if self.ib.isConnected():
            if self._status in {"not_connected", "gateway_unavailable"}:
                self._stamp("connected", data_lost=False)
            return self.session_health(reconnect=False)
        try:
            self.ib.connect(
                self.config.host,
                self.config.port,
                clientId=self.config.client_id,
                timeout=self.config.connect_timeout_seconds,
                readonly=True,
                fetchFields=StartupFetch(0),
            )
        except Exception as exc:
            self._stamp(
                "gateway_unavailable",
                message=(
                    f"{self.config.host}:{self.config.port} is not listening; "
                    f"IB Gateway may be at the daily login screen: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            return self.session_health(reconnect=False)
        if not self.ib.isConnected():
            self._stamp(
                "gateway_unavailable",
                message=(
                    f"{self.config.host}:{self.config.port} did not report connected"
                ),
            )
        return self.session_health(reconnect=False)

    def _ensure_connected(self) -> None:
        health = self.connect()
        if not health["connected"]:
            raise IbkrGatewayUnavailable(str(health["message"]))

    def session_health(self, *, reconnect: bool = True) -> dict[str, Any]:
        if reconnect and not self.ib.isConnected():
            return self.connect()
        connected = bool(self.ib.isConnected())
        server_version = (
            int(self.ib.client.serverVersion()) if connected else None
        )
        accounts = (
            [str(account) for account in self.ib.managedAccounts()]
            if connected
            else []
        )
        return {
            "connected": connected,
            "status": self._status,
            "host": self.config.host,
            "port": self.config.port,
            "client_id": self.config.client_id,
            "server_version": server_version,
            "accounts": accounts,
            "last_error_code": self._last_error_code,
            "message": self._last_error_message,
            "data_lost": self._data_lost,
            "last_event_at": (
                self._last_event_at.isoformat()
                if self._last_event_at is not None
                else None
            ),
        }

    def resolve_umc_contract(self) -> IbkrContractDetails:
        self._ensure_connected()
        detail = self._resolve_umc_detail()
        contract = detail.contract
        return IbkrContractDetails(
            con_id=int(contract.conId),
            symbol=str(contract.symbol),
            exchange=str(contract.exchange),
            primary_exchange=str(contract.primaryExchange),
            currency=str(contract.currency),
            long_name=str(detail.longName),
            time_zone_id=str(detail.timeZoneId),
            trading_hours=str(detail.tradingHours),
            liquid_hours=str(detail.liquidHours),
        )

    def _resolve_umc_detail(self) -> Any:
        if self._umc_detail is None:
            requested = Stock("UMC", "SMART", "USD", primaryExchange="NYSE")
            matches = list(self.ib.reqContractDetails(requested))
            if len(matches) != 1:
                raise IbkrWorkerError(
                    "UMC contract resolution must return exactly one match; "
                    f"received {len(matches)}"
                )
            self._umc_detail = matches[0]
        return self._umc_detail

    def fetch_umc_quote(
        self,
        *,
        quote_wait_timeout_seconds: float,
    ) -> dict[str, Any]:
        if quote_wait_timeout_seconds <= 0:
            raise ValueError("quote_wait_timeout_seconds must be positive")
        self._ensure_connected()
        contract = self._resolve_umc_detail().contract
        self.ib.reqMarketDataType(3)
        ticker = self.ib.reqMktData(
            contract,
            genericTickList="",
            snapshot=False,
            regulatorySnapshot=False,
        )
        remaining = float(quote_wait_timeout_seconds)
        try:
            while remaining > 0:
                tier = getattr(ticker, "marketDataType", None)
                values = (
                    getattr(ticker, "last", None),
                    getattr(ticker, "close", None),
                    getattr(ticker, "bid", None),
                    getattr(ticker, "ask", None),
                )
                if tier is not None and any(_is_finite(value) for value in values):
                    break
                wait_slice = min(0.2, remaining)
                self.ib.sleep(wait_slice)
                remaining -= wait_slice
            return {
                "con_id": int(contract.conId),
                "market_data_tier": getattr(ticker, "marketDataType", None),
                "last": getattr(ticker, "last", None),
                "close": getattr(ticker, "close", None),
                "bid": getattr(ticker, "bid", None),
                "ask": getattr(ticker, "ask", None),
                "bid_size": getattr(ticker, "bidSize", None),
                "ask_size": getattr(ticker, "askSize", None),
                "ticker_time": getattr(ticker, "time", None),
                "last_timestamp": getattr(ticker, "lastTimestamp", None),
                "delayed_last_timestamp": getattr(
                    ticker,
                    "delayedLastTimestamp",
                    None,
                ),
                "observed_at": self.clock(),
            }
        finally:
            self.ib.cancelMktData(contract)

    def close(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
        self._stamp("closed")


def _ibkr_worker(
    connection: Connection,
    connection_config: IbkrConnectionConfig,
) -> None:
    client = _IbkrWorkerClient(connection_config)
    try:
        while True:
            request = connection.recv()
            operation = str(request["operation"])
            args = tuple(request.get("args", ()))
            kwargs = dict(request.get("kwargs", {}))
            should_stop = operation == "close"
            try:
                result = getattr(client, operation)(*args, **kwargs)
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
            client.close()
        except BaseException:
            pass
        connection.close()


class IbkrClientProcess:
    """Process-isolated, read-only IBKR connection and contract facade."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = DEFAULT_CLIENT_ID,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        worker_target: Callable[..., None] = _ibkr_worker,
    ) -> None:
        self.connection_config = IbkrConnectionConfig(
            host=host,
            port=port,
            client_id=client_id,
            connect_timeout_seconds=connect_timeout_seconds,
        )
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.terminate_timeout_seconds = float(terminate_timeout_seconds)
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.terminate_timeout_seconds < 0:
            raise ValueError("terminate_timeout_seconds must not be negative")
        self.worker_target = worker_target
        self._transport = SubprocessTransport(
            worker_target=self.worker_target,
            worker_args=(self.connection_config,),
            process_name="project-lux-ibkr-readonly",
            broker_label="IBKR",
            worker_label="IBKR readonly",
            closed_message="IBKR client process is closed",
            error_type=IbkrWorkerError,
            timeout_type=IbkrWorkerTimeout,
            terminate_timeout_seconds=self.terminate_timeout_seconds,
        )

    @property
    def worker_pid(self) -> int | None:
        return self._transport.worker_pid

    def connect(self) -> dict[str, Any]:
        return dict(self._request_guarded("connect"))

    def resolve_umc_contract(self) -> IbkrContractDetails:
        result = self._request_guarded("resolve_umc_contract")
        if not isinstance(result, IbkrContractDetails):
            raise IbkrWorkerError(
                f"unexpected contract details type: {type(result).__name__}"
            )
        return result

    def fetch_umc_quote(
        self,
        *,
        quote_wait_timeout_seconds: float,
    ) -> dict[str, Any]:
        return dict(
            self._request_guarded(
                "fetch_umc_quote",
                quote_wait_timeout_seconds=quote_wait_timeout_seconds,
            )
        )

    def session_health(self) -> dict[str, Any]:
        health = dict(self._request_guarded("session_health"))
        health["worker_pid"] = self.worker_pid
        return health

    def restart_worker(self) -> None:
        self._transport.restart(require_open=True)

    def close(self) -> None:
        self._transport.close(
            operation="close",
            payload={"operation": "close"},
            timeout=self.terminate_timeout_seconds,
        )

    def _request_guarded(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        with self._transport.lock:
            try:
                return self._transport.request(
                    operation,
                    payload={
                        "operation": operation,
                        "args": args,
                        "kwargs": kwargs,
                    },
                    timeout=self.request_timeout_seconds,
                )
            except (IbkrWorkerTimeout, IbkrWorkerError):
                self._transport.terminate()
                raise

    def __enter__(self) -> "IbkrClientProcess":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "DEFAULT_TERMINATE_TIMEOUT_SECONDS",
    "IbkrClientProcess",
    "IbkrConnectionConfig",
    "IbkrContractDetails",
    "IbkrGatewayUnavailable",
    "IbkrWorkerError",
    "IbkrWorkerTimeout",
]
