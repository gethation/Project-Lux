"""The read-only UMC probe: does live market data actually reach the API?

This exists because the question is not answerable from the Gateway screen.
IBKR treats API-delivered data as off-platform and entitles it separately, so a
subscription that lights up TWS can leave the API on delayed -- or on nothing.

Two properties are load-bearing and were both wrong before 2026-08-03:

  It asks for LIVE. Requesting delayed OVERRIDES an entitlement the account
  actually holds, so a probe pinned to delayed can only ever answer "delayed",
  which is the one answer nobody needs.

  It reports the BOOK. This branch scores entries on a directional bid/ask
  z-score, so a `last` price with no bid/ask means every bar reports
  missing_book and the pair takes zero entries. A probe that reports only
  `last` calls that situation healthy.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Callable

from ib_async import IB, Stock, StartupFetch


DEFAULT_DIAGNOSTIC_CLIENT_ID = 17_001

LIVE_MARKET_DATA_TYPE = 1

# 236 = shortable shares. A margin account is not the same thing as a
# borrowable symbol, and roughly half this strategy's trades sell UMC first.
SHORTABLE_GENERIC_TICK = "236"

# IBKR's shortable rank, as documented with tick 236.
SHORTABLE_AVAILABLE_RANK = 2.5

# IBKR's "you are not entitled to this" family. A missing book WITH one of
# these attached is a subscription problem; a missing book WITHOUT one is a
# closed market or a connectivity problem. They have different fixes, so the
# probe must not collapse them into one failure.
MARKET_DATA_ENTITLEMENT_ERROR_CODES = frozenset({354, 10089, 10090, 10167, 10197})

# IBKR sends its data-farm status notices ("HMDS data farm connection is OK",
# 2104/2106/2107/2158) through the SAME error channel as real failures. They
# arrive on every healthy connection, so surfacing them as warnings teaches the
# reader to skim past the line that will one day matter.
INFORMATIONAL_ERROR_CODE_RANGE = (2100, 2200)


def _is_informational(code: int) -> bool:
    low, high = INFORMATIONAL_ERROR_CODE_RANGE
    return low <= code <= high


class IbkrConnectivityError(RuntimeError):
    """The read-only IBKR connectivity diagnostic could not complete."""


@dataclass(frozen=True)
class IbkrDiagnosticConfig:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = DEFAULT_DIAGNOSTIC_CLIENT_ID
    symbol: str = "UMC"
    # Live by default. Overriding this to 3 is how the entitlement gets hidden.
    market_data_type: int = LIVE_MARKET_DATA_TYPE
    connect_timeout_seconds: float = 8.0
    # Generous: the book lands in well under a second, but tick 236 took ~5s
    # against the real Gateway and the probe should not report it as absent
    # merely because it did not wait.
    quote_wait_timeout_seconds: float = 20.0
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
    market_data_type_requested: int
    market_data_tier: int | None
    market_data_tier_label: str
    quote_last: float | None
    quote_close: float | None
    quote_bid: float | None
    quote_ask: float | None
    quote_bid_size: float | None
    quote_ask_size: float | None
    shortable_shares: float | None
    shortable_rank: float | None
    entitlement_error_code: int | None
    entitlement_error_message: str | None
    historical_bar_count: int
    historical_error_code: int | None
    historical_error_message: str | None

    @property
    def book_available(self) -> bool:
        """Whether the directional z-score can be computed at all.

        This is the whole point of the probe. Everything else is context.
        """
        return self.quote_bid is not None and self.quote_ask is not None

    @property
    def live_tier_granted(self) -> bool:
        return self.market_data_tier == LIVE_MARKET_DATA_TYPE

    @property
    def shortable(self) -> bool | None:
        """None when IBKR did not publish tick 236 -- unknown, not 'no'."""
        if self.shortable_rank is None:
            return None
        return self.shortable_rank >= SHORTABLE_AVAILABLE_RANK

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["accounts"] = list(self.accounts)
        payload["book_available"] = self.book_available
        payload["live_tier_granted"] = self.live_tier_granted
        payload["shortable"] = self.shortable
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
    if not config.symbol.strip():
        raise ValueError("symbol must not be empty")
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

        requested = Stock(
            config.symbol, "SMART", "USD", primaryExchange="NYSE"
        )
        details = list(ib.reqContractDetails(requested))
        if len(details) != 1:
            raise IbkrConnectivityError(
                f"{config.symbol} contract resolution must return exactly one "
                f"match; received {len(details)}"
            )
        detail = details[0]
        contract = detail.contract

        quote_error_start = len(errors)
        ib.reqMarketDataType(config.market_data_type)
        ticker = ib.reqMktData(
            contract,
            genericTickList=SHORTABLE_GENERIC_TICK,
            snapshot=False,
            regulatorySnapshot=False,
        )
        remaining = config.quote_wait_timeout_seconds
        try:
            while remaining > 0:
                tier = getattr(ticker, "marketDataType", None)
                bid = _finite_float(getattr(ticker, "bid", None))
                ask = _finite_float(getattr(ticker, "ask", None))
                shortable = _finite_float(getattr(ticker, "shortableShares", None))
                if tier is not None and bid is not None and ask is not None:
                    # The book is what gates the strategy; tick 236 is context.
                    # Stop once both have landed, but do not let a symbol that
                    # never publishes 236 burn the whole budget.
                    if shortable is not None:
                        break
                wait_slice = min(0.2, remaining)
                ib.sleep(wait_slice)
                remaining -= wait_slice
        finally:
            ib.cancelMktData(contract)

        quote_errors = errors[quote_error_start:]
        entitlement_error = next(
            (
                (code, message)
                for _, code, message in quote_errors
                if code in MARKET_DATA_ENTITLEMENT_ERROR_CODES
            ),
            None,
        )

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
                if code >= 100 and not _is_informational(code)
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
            market_data_type_requested=int(config.market_data_type),
            market_data_tier=tier,
            market_data_tier_label=market_data_tier_label(tier),
            quote_last=_finite_float(getattr(ticker, "last", None)),
            quote_close=_finite_float(getattr(ticker, "close", None)),
            quote_bid=_finite_float(getattr(ticker, "bid", None)),
            quote_ask=_finite_float(getattr(ticker, "ask", None)),
            quote_bid_size=_finite_float(getattr(ticker, "bidSize", None)),
            quote_ask_size=_finite_float(getattr(ticker, "askSize", None)),
            shortable_shares=_finite_float(getattr(ticker, "shortableShares", None)),
            shortable_rank=_finite_float(getattr(ticker, "shortable", None)),
            entitlement_error_code=(
                entitlement_error[0] if entitlement_error is not None else None
            ),
            entitlement_error_message=(
                entitlement_error[1] if entitlement_error is not None else None
            ),
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
