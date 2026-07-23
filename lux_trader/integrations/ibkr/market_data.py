from __future__ import annotations

import math
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from ...core.time import TAIPEI_TZ, ensure_taipei
from ...market_data.types import LiveQuote
from .client_process import IbkrClientProcess, IbkrWorkerError
from .diagnostic import market_data_tier_label


DEFAULT_QUOTE_WAIT_TIMEOUT_SECONDS = 10.0


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _quote_timestamp(payload: dict[str, Any]) -> datetime:
    for key in ("delayed_last_timestamp", "last_timestamp", "ticker_time"):
        value = payload.get(key)
        if isinstance(value, datetime):
            return ensure_taipei(value)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return datetime.fromtimestamp(float(value), tz=UTC).astimezone(TAIPEI_TZ)
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            return ensure_taipei(parsed)
    return ensure_taipei(payload["observed_at"])


class IbkrUmcQuoteProvider:
    """UMC delayed/live quote provider matching the existing QuoteProvider shape."""

    def __init__(
        self,
        client: IbkrClientProcess | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 17_002,
        connect_timeout_seconds: float = 8.0,
        request_timeout_seconds: float = 60.0,
        terminate_timeout_seconds: float = 3.0,
        quote_wait_timeout_seconds: float = DEFAULT_QUOTE_WAIT_TIMEOUT_SECONDS,
        warning_stream: TextIO | None = None,
    ) -> None:
        if quote_wait_timeout_seconds <= 0:
            raise ValueError("quote_wait_timeout_seconds must be positive")
        self.client = client or IbkrClientProcess(
            host=host,
            port=port,
            client_id=client_id,
            connect_timeout_seconds=connect_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            terminate_timeout_seconds=terminate_timeout_seconds,
        )
        self._owns_client = client is None
        self.quote_wait_timeout_seconds = float(quote_wait_timeout_seconds)
        self.warning_stream = warning_stream or sys.stderr
        self._warned_tiers: set[int] = set()
        self.last_market_data_tier: int | None = None

    def fetch_quote(self, symbol: str) -> LiveQuote:
        if symbol.upper() != "UMC":
            raise ValueError(f"IBKR UMC provider does not serve symbol {symbol!r}")
        payload = dict(
            self.client.fetch_umc_quote(
                quote_wait_timeout_seconds=self.quote_wait_timeout_seconds
            )
        )
        tier_value = payload.get("market_data_tier")
        tier = int(tier_value) if tier_value is not None else None
        if tier is None:
            raise IbkrWorkerError(
                "IBKR UMC quote did not report the served market-data tier"
            )
        self.last_market_data_tier = tier
        delayed = tier in {3, 4}
        if tier != 1 and tier not in self._warned_tiers:
            self._write_non_live_banner(tier)
            self._warned_tiers.add(tier)

        last = _finite_float(payload.get("last"))
        close = _finite_float(payload.get("close"))
        bid = _finite_float(payload.get("bid"))
        ask = _finite_float(payload.get("ask"))
        price = last
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / 2.0
        if price is None:
            price = bid if bid is not None else ask
        if price is None:
            price = close
        if price is None:
            raise IbkrWorkerError(f"IBKR UMC quote has no usable price: {payload}")

        raw = {
            **payload,
            "market_data_tier": tier,
            "market_data_tier_label": market_data_tier_label(tier),
            "is_delayed": delayed,
        }
        return LiveQuote(
            source="ibkr_umc",
            symbol="UMC",
            timestamp=_quote_timestamp(payload),
            price=price,
            bid=bid,
            ask=ask,
            bid_size=_finite_float(payload.get("bid_size")),
            ask_size=_finite_float(payload.get("ask_size")),
            raw=raw,
            market_data_tier=tier,
            is_delayed=delayed,
        )

    def market_data_status(self) -> dict[str, Any]:
        tier = self.last_market_data_tier
        return {
            "market_data_tier": tier,
            "market_data_tier_label": market_data_tier_label(tier),
            "is_delayed": tier in {3, 4},
        }

    def session_health(self) -> dict[str, Any]:
        health = dict(self.client.session_health())
        health.update(self.market_data_status())
        return health

    def reconnect(self) -> dict[str, Any]:
        return dict(self.client.session_health())

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _write_non_live_banner(self, tier: int) -> None:
        label = market_data_tier_label(tier).upper()
        print("=" * 72, file=self.warning_stream)
        if tier in {3, 4}:
            print(
                f"WARNING: DELAYED MARKET DATA ({label}) - NOT LIVE",
                file=self.warning_stream,
            )
        else:
            print(
                f"WARNING: NON-LIVE MARKET DATA ({label})",
                file=self.warning_stream,
            )
        print("=" * 72, file=self.warning_stream)

    def __enter__(self) -> "IbkrUmcQuoteProvider":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_QUOTE_WAIT_TIMEOUT_SECONDS",
    "IbkrUmcQuoteProvider",
]
