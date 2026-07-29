from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ib_async import IB, Stock, StartupFetch


DEFAULT_DIAGNOSTIC_CLIENT_ID = 17_001


class IbkrConnectivityError(RuntimeError):
    """The read-only IBKR connectivity diagnostic could not complete."""


@dataclass(frozen=True)
class IbkrDiagnosticConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = DEFAULT_DIAGNOSTIC_CLIENT_ID
    connect_timeout_seconds: float = 8.0
    quote_wait_timeout_seconds: float = 10.0
    historical_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class IbkrDiagnosticResult:
    host: str
    port: int
    client_id: int
    connected: bool
    server_version: int
    accounts: tuple[str, ...]
    con_id: int
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    long_name: str
    time_zone_id: str
    trading_hours: str
    market_data_tier: int | None
    market_data_tier_label: str
    quote_last: float | None
    quote_close: float | None
    historical_bar_count: int
    historical_error_code: int | None
    historical_error_message: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["accounts"] = list(self.accounts)
        return payload


def market_data_tier_label(tier: int | None) -> str:
    return {
        1: "live",
        2: "frozen",
        3: "delayed",
        4: "delayed-frozen",
    }.get(tier, "unknown")


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def run_connectivity_diagnostic(
    config: IbkrDiagnosticConfig = IbkrDiagnosticConfig(),
    *,
    ib_factory: Callable[[], Any] = IB,
) -> IbkrDiagnosticResult:
    """Run the maintained UMC read-only connectivity and data probe."""

    if not config.host.strip():
        raise ValueError("host must not be empty")
    if config.port <= 0:
        raise ValueError("port must be positive")
    if config.client_id < 0:
        raise ValueError("client_id must not be negative")
    for name, value in (
        ("connect_timeout_seconds", config.connect_timeout_seconds),
        ("quote_wait_timeout_seconds", config.quote_wait_timeout_seconds),
        ("historical_timeout_seconds", config.historical_timeout_seconds),
    ):
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    ib = ib_factory()
    errors: list[tuple[int, int, str]] = []

    def record_error(
        request_id: int,
        error_code: int,
        error_message: str,
        _contract: object,
    ) -> None:
        errors.append((int(request_id), int(error_code), str(error_message)))

    ib.errorEvent += record_error
    try:
        try:
            ib.connect(
                config.host,
                config.port,
                clientId=config.client_id,
                timeout=config.connect_timeout_seconds,
                readonly=True,
                fetchFields=StartupFetch(0),
            )
        except Exception as exc:
            raise IbkrConnectivityError(
                "IBKR Gateway is unavailable at "
                f"{config.host}:{config.port}; it may be at the daily login screen: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if not ib.isConnected():
            raise IbkrConnectivityError(
                f"IBKR did not report connected at {config.host}:{config.port}"
            )

        requested = Stock("UMC", "SMART", "USD", primaryExchange="NYSE")
        details = list(ib.reqContractDetails(requested))
        if len(details) != 1:
            raise IbkrConnectivityError(
                "UMC contract resolution must return exactly one match; "
                f"received {len(details)}"
            )
        detail = details[0]
        contract = detail.contract

        ib.reqMarketDataType(3)
        ticker = ib.reqMktData(
            contract,
            genericTickList="",
            snapshot=False,
            regulatorySnapshot=False,
        )
        remaining = config.quote_wait_timeout_seconds
        try:
            while remaining > 0:
                tier = getattr(ticker, "marketDataType", None)
                last = _finite_float(getattr(ticker, "last", None))
                close = _finite_float(getattr(ticker, "close", None))
                if tier is not None and (last is not None or close is not None):
                    break
                wait_slice = min(0.2, remaining)
                ib.sleep(wait_slice)
                remaining -= wait_slice
        finally:
            ib.cancelMktData(contract)

        historical_error_start = len(errors)
        bars = list(
            ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
                keepUpToDate=False,
                timeout=config.historical_timeout_seconds,
            )
        )
        historical_errors = errors[historical_error_start:]
        historical_error = next(
            (
                (code, message)
                for _, code, message in reversed(historical_errors)
                if code >= 100
            ),
            None,
        )

        tier_value = getattr(ticker, "marketDataType", None)
        tier = int(tier_value) if tier_value is not None else None
        return IbkrDiagnosticResult(
            host=config.host,
            port=config.port,
            client_id=config.client_id,
            connected=True,
            server_version=int(ib.client.serverVersion()),
            accounts=tuple(str(item) for item in ib.managedAccounts()),
            con_id=int(contract.conId),
            symbol=str(contract.symbol),
            exchange=str(contract.exchange),
            primary_exchange=str(contract.primaryExchange),
            currency=str(contract.currency),
            long_name=str(detail.longName),
            time_zone_id=str(detail.timeZoneId),
            trading_hours=str(detail.tradingHours),
            market_data_tier=tier,
            market_data_tier_label=market_data_tier_label(tier),
            quote_last=_finite_float(getattr(ticker, "last", None)),
            quote_close=_finite_float(getattr(ticker, "close", None)),
            historical_bar_count=len(bars),
            historical_error_code=(
                historical_error[0] if historical_error is not None else None
            ),
            historical_error_message=(
                historical_error[1] if historical_error is not None else None
            ),
        )
    finally:
        try:
            ib.errorEvent -= record_error
        except Exception:
            pass
        if ib.isConnected():
            ib.disconnect()
