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


# How long to let the event loop run per steady-state quote so queued ticks
# are applied. Short on purpose: the live loop polls once a second.
_QUOTE_PUMP_SECONDS = 0.1

# IBKR market-data types. Requesting 3 (delayed) OVERRIDES an entitlement you
# actually hold -- the subscription cannot upgrade what the client explicitly
# asked for -- so the default is 1 and an unentitled session fails loudly.
#
# Under this branch's directional bid/ask signal, delayed data is useless
# regardless: the delayed tier serves no book, so every bar reports missing_book
# and the pair takes no entries. Asking for 3 buys a `last` price nothing can
# trade on, at the cost of hiding whether the entitlement ever arrived.
# Terminal IBKR order statuses that mean the order will never fill. Anything
# outside this set that is not "Filled" is ambiguous, not safe.
TERMINAL_UNFILLED_STATUSES = frozenset({"Cancelled", "ApiCancelled", "Inactive"})

LIVE_MARKET_DATA_TYPE = 1
FROZEN_MARKET_DATA_TYPE = 2
DELAYED_MARKET_DATA_TYPE = 3
DELAYED_FROZEN_MARKET_DATA_TYPE = 4
MARKET_DATA_TYPES = frozenset(
    {
        LIVE_MARKET_DATA_TYPE,
        FROZEN_MARKET_DATA_TYPE,
        DELAYED_MARKET_DATA_TYPE,
        DELAYED_FROZEN_MARKET_DATA_TYPE,
    }
)


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
    market_data_type: int = LIVE_MARKET_DATA_TYPE
    # Defaults to a read-only session, so a client that never intends to trade
    # cannot place an order even through a bug. Order placement has to ask for
    # it by name; see integrations/ibkr/execution.py.
    readonly: bool = True

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if self.port <= 0:
            raise ValueError("port must be positive")
        if self.client_id < 0:
            raise ValueError("client_id must not be negative")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        if self.market_data_type not in MARKET_DATA_TYPES:
            raise ValueError(
                f"market_data_type must be one of {sorted(MARKET_DATA_TYPES)}; "
                f"got {self.market_data_type}"
            )


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
        # One streaming subscription is opened lazily and kept for the process's
        # lifetime; see _ensure_umc_subscription for why per-quote subscribe and
        # cancel is actively harmful.
        self._umc_ticker: Any | None = None
        self._umc_ticker_time: Any | None = None
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
        # The ticker belongs to the session that just died; holding it would
        # serve that session's last values forever after a reconnect.
        self._umc_ticker = None
        self._umc_ticker_time = None
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
                readonly=self.config.readonly,
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

    def _ensure_umc_subscription(self) -> Any:
        """Open the streaming subscription once and keep it.

        Subscribing and cancelling per quote looks harmless but is not: ib_async
        returns the SAME cached Ticker for a contract, and cancelMktData does
        not clear its fields. A wait loop that stops as soon as any finite value
        is present therefore returns the PREVIOUS call's snapshot instantly,
        forever. Measured 2026-07-25: 485 consecutive fetches returned a
        byte-identical payload, with ticker.time frozen at the first fetch.
        The staleness gate caught it, which is the only reason it was visible.

        Keeping one subscription also stops the 485 subscribe/cancel cycles that
        wedged the Gateway badly enough to refuse fresh client ids.
        """
        if self._umc_ticker is None:
            contract = self._resolve_umc_detail().contract
            self.ib.reqMarketDataType(self.config.market_data_type)
            self._umc_ticker = self.ib.reqMktData(
                contract,
                genericTickList="",
                snapshot=False,
                regulatorySnapshot=False,
            )
        return self._umc_ticker

    def _cancel_umc_subscription(self) -> None:
        if self._umc_ticker is None:
            return
        self._umc_ticker = None
        self._umc_ticker_time = None
        if self._umc_detail is None or not self.ib.isConnected():
            return
        try:
            self.ib.cancelMktData(self._umc_detail.contract)
        except Exception:
            # Teardown must never mask the caller's own failure.
            pass

    def fetch_umc_quote(
        self,
        *,
        quote_wait_timeout_seconds: float,
    ) -> dict[str, Any]:
        if quote_wait_timeout_seconds <= 0:
            raise ValueError("quote_wait_timeout_seconds must be positive")
        self._ensure_connected()
        contract = self._resolve_umc_detail().contract
        ticker = self._ensure_umc_subscription()
        previous_time = self._umc_ticker_time

        def has_values() -> bool:
            return any(
                _is_finite(getattr(ticker, name, None))
                for name in ("last", "close", "bid", "ask")
            )

        if previous_time is None:
            # Bootstrap only: a brand-new subscription has no values yet, so the
            # first call waits for the stream to deliver something.
            remaining = float(quote_wait_timeout_seconds)
            while remaining > 0:
                if getattr(ticker, "marketDataType", None) is not None and has_values():
                    break
                wait_slice = min(0.2, remaining)
                self.ib.sleep(wait_slice)
                remaining -= wait_slice
        else:
            # Steady state: pump the event loop briefly so queued ticks are
            # applied, then read whatever the stream currently holds. Blocking
            # until the ticker advances would stall the live loop whenever the
            # market is quiet -- and a quiet market is not an error. The quote's
            # timestamp stays honest, so the staleness budget decides.
            self.ib.sleep(_QUOTE_PUMP_SECONDS)

        ticker_time = getattr(ticker, "time", None)
        advanced = previous_time is None or ticker_time != previous_time
        self._umc_ticker_time = ticker_time
        return {
            "con_id": int(contract.conId),
            "market_data_tier": getattr(ticker, "marketDataType", None),
            "last": getattr(ticker, "last", None),
            "close": getattr(ticker, "close", None),
            "bid": getattr(ticker, "bid", None),
            "ask": getattr(ticker, "ask", None),
            "bid_size": getattr(ticker, "bidSize", None),
            "ask_size": getattr(ticker, "askSize", None),
            "ticker_time": ticker_time,
            # Whether ib_async applied a new tick since the previous fetch. Not
            # an error on its own -- a quiet market does not tick -- but a long
            # run of False while the session is open means the stream is wedged.
            "ticker_advanced": advanced,
            "last_timestamp": getattr(ticker, "lastTimestamp", None),
            "delayed_last_timestamp": getattr(
                ticker,
                "delayedLastTimestamp",
                None,
            ),
            "observed_at": self.clock(),
        }

    def fetch_umc_historical_1m(
        self,
        *,
        end_date_time: str,
        duration: str,
        use_rth: bool = True,
        timeout_seconds: float = 60.0,
    ) -> list[dict[str, Any]]:
        """Raw 1-minute TRADES bars for UMC.

        ``formatDate=2`` is deliberate and must not be relaxed: any other value
        makes IBKR return timestamps in whatever timezone the Gateway login screen
        happens to be set to, which would silently redefine every stored bar.
        ``TRADES`` is likewise required -- this account has no bid/ask permission,
        so ``MIDPOINT`` fails with error 162.
        """
        self._ensure_connected()
        contract = self._resolve_umc_detail().contract
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime=end_date_time,
            durationStr=duration,
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=use_rth,
            formatDate=2,
            timeout=timeout_seconds,
        )
        return [
            {
                "date": bar.date,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
            for bar in bars
        ]

    def fetch_account_snapshot(self) -> dict[str, Any]:
        """Read-only positions, open orders, and account values."""
        self._ensure_connected()
        accounts = [str(account) for account in self.ib.managedAccounts()]
        positions = [
            {
                "account": str(position.account),
                "symbol": str(position.contract.symbol),
                "sec_type": str(position.contract.secType),
                "currency": str(position.contract.currency),
                "con_id": int(position.contract.conId),
                "quantity": float(position.position),
                "avg_cost": float(position.avgCost),
            }
            for position in self.ib.positions()
        ]
        open_orders = [
            {
                "order_id": int(trade.order.orderId),
                "symbol": str(trade.contract.symbol),
                "action": str(trade.order.action),
                "quantity": float(trade.order.totalQuantity),
                "status": str(trade.orderStatus.status),
            }
            for trade in self.ib.openTrades()
        ]
        values = [
            {
                "account": str(row.account),
                "tag": str(row.tag),
                "value": str(row.value),
                "currency": str(row.currency),
            }
            for row in self.ib.accountSummary()
        ]
        return {
            "accounts": accounts,
            "positions": positions,
            "open_orders": open_orders,
            "account_values": values,
            "fetched_at": self.clock(),
        }

    # -- orders ------------------------------------------------------------
    #
    # Confirmation is tiered here rather than in the parent, because the Trade
    # object and its events live in this process. Mirrors the Fubon adapter's
    # design: event first, query second, position delta last.

    def fetch_umc_position(self) -> float:
        """Signed UMC share count, summed over any split rows."""
        self._ensure_connected()
        total = 0.0
        for position in self.ib.positions():
            if str(position.contract.symbol).strip().upper() == "UMC":
                total += float(position.position)
        return total

    def fetch_umc_open_orders(self) -> list[dict[str, Any]]:
        self._ensure_connected()
        return [
            {
                "order_id": int(trade.order.orderId),
                "action": str(trade.order.action),
                "quantity": float(trade.order.totalQuantity),
                "status": str(trade.orderStatus.status),
                "filled": float(trade.orderStatus.filled),
                "remaining": float(trade.orderStatus.remaining),
            }
            for trade in self.ib.openTrades()
            if str(trade.contract.symbol).strip().upper() == "UMC"
        ]

    def place_and_confirm_umc_order(
        self,
        *,
        action: str,
        quantity: float,
        wait_seconds: float,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        """Place one market order and report what is KNOWN about its outcome.

        Returns a classification, never a guess:

          filled     the order reached a terminal filled state
          failed     terminal, zero filled, and the position did not move --
                     safe, because nothing was left open
          unknown    anything else, including a timeout or a partial. The caller
                     must treat this as possible exposure and pause.

        The failed/unknown split is the whole point. "Rejected" and "no idea"
        are both non-fills, but only one of them is safe to retry.
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if float(quantity) != int(quantity):
            raise ValueError(f"UMC is a cash equity; got {quantity} shares")
        self._ensure_connected()

        from ib_async import MarketOrder

        contract = self._resolve_umc_detail().contract
        position_before = self.fetch_umc_position()
        order = MarketOrder(action, int(quantity))

        submitted_at = self.clock()
        trade = self.ib.placeOrder(contract, order)

        # Tier 1 -- events. isDone() flips when a terminal status arrives.
        deadline = float(wait_seconds)
        while deadline > 0 and not trade.isDone():
            slice_seconds = min(float(poll_seconds), deadline)
            self.ib.sleep(slice_seconds)
            deadline -= slice_seconds

        status = str(trade.orderStatus.status)
        filled = float(trade.orderStatus.filled)
        remaining = float(trade.orderStatus.remaining)

        # Tier 2 -- query. An event can be missed; the order book cannot.
        if not trade.isDone():
            for row in self.fetch_umc_open_orders():
                if row["order_id"] == int(trade.order.orderId):
                    status, filled, remaining = (
                        row["status"],
                        row["filled"],
                        row["remaining"],
                    )
                    break

        # Tier 3 -- position delta. Last resort, and only trusted when it
        # agrees exactly with the order: a delta that half-matches says the
        # position moved for some other reason too, which is not a confirmation.
        position_after = self.fetch_umc_position()
        expected_delta = int(quantity) if action.upper() == "BUY" else -int(quantity)
        observed_delta = position_after - position_before
        delta_confirms = abs(observed_delta - expected_delta) <= 1e-9

        if status == "Filled" or (filled >= int(quantity) and remaining <= 0):
            classification = "filled"
            filled = max(filled, float(quantity))
        elif delta_confirms:
            # The broker never said so, but the account moved by exactly this
            # order. Treat as filled and say which tier decided it.
            classification = "filled"
            filled = float(quantity)
        elif (
            status in TERMINAL_UNFILLED_STATUSES
            and filled == 0.0
            and observed_delta == 0.0
        ):
            classification = "failed"
        else:
            classification = "unknown"

        return {
            "classification": classification,
            "order_id": int(trade.order.orderId),
            "status": status,
            "filled": filled,
            "remaining": remaining,
            "avg_fill_price": float(trade.orderStatus.avgFillPrice or 0.0),
            "position_before": position_before,
            "position_after": position_after,
            "observed_delta": observed_delta,
            "expected_delta": expected_delta,
            "delta_confirms": delta_confirms,
            "submitted_at": submitted_at,
            "resolved_at": self.clock(),
        }

    def close(self) -> None:
        self._cancel_umc_subscription()
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
    """Process-isolated IBKR connection and contract facade.

    Read-only unless ``readonly=False`` is passed, which only the execution
    adapter does, and only behind env gates.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = DEFAULT_CLIENT_ID,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        terminate_timeout_seconds: float = DEFAULT_TERMINATE_TIMEOUT_SECONDS,
        market_data_type: int = LIVE_MARKET_DATA_TYPE,
        readonly: bool = True,
        worker_target: Callable[..., None] = _ibkr_worker,
    ) -> None:
        self.connection_config = IbkrConnectionConfig(
            host=host,
            port=port,
            client_id=client_id,
            connect_timeout_seconds=connect_timeout_seconds,
            market_data_type=market_data_type,
            readonly=readonly,
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
            process_name=(
                "project-lux-ibkr-readonly" if readonly else "project-lux-ibkr-exec"
            ),
            broker_label="IBKR",
            worker_label="IBKR readonly" if readonly else "IBKR execution",
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

    def fetch_umc_historical_1m(
        self,
        *,
        end_date_time: str = "",
        duration: str = "1 D",
        use_rth: bool = True,
        timeout_seconds: float = 60.0,
    ) -> list[dict[str, Any]]:
        result = self._request_guarded(
            "fetch_umc_historical_1m",
            end_date_time=end_date_time,
            duration=duration,
            use_rth=use_rth,
            timeout_seconds=timeout_seconds,
        )
        return [dict(row) for row in result]

    def fetch_account_snapshot(self) -> dict[str, Any]:
        return dict(self._request_guarded("fetch_account_snapshot"))

    def fetch_umc_position(self) -> float:
        return float(self._request_guarded("fetch_umc_position"))

    def fetch_umc_open_orders(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._request_guarded("fetch_umc_open_orders")]

    def place_and_confirm_umc_order(
        self,
        *,
        action: str,
        quantity: float,
        wait_seconds: float,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        return dict(
            self._request_guarded(
                "place_and_confirm_umc_order",
                action=action,
                quantity=quantity,
                wait_seconds=wait_seconds,
                poll_seconds=poll_seconds,
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
