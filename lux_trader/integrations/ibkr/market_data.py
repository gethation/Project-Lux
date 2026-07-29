from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

import pandas as pd

from ...core.time import TAIPEI_TZ, ensure_taipei
from ...market_data.types import LiveQuote
from .client_process import (
    LIVE_MARKET_DATA_TYPE,
    IbkrClientProcess,
    IbkrWorkerError,
)
from .diagnostic import market_data_tier_label
from .historical import _format_end, bars_to_frame


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
        market_data_type: int = LIVE_MARKET_DATA_TYPE,
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
            market_data_type=market_data_type,
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
        bid = _finite_float(payload.get("bid"))
        ask = _finite_float(payload.get("ask"))
        price = last
        if price is None and bid is not None and ask is not None:
            price = (bid + ask) / 2.0
        if price is None:
            price = bid if bid is not None else ask
        # `close` is deliberately NOT a fallback. It is the PREVIOUS session's
        # close, and _quote_timestamp stamps a quote with observed_at when the
        # payload carries no tick time -- so falling back to it would hand the
        # staleness gate a yesterday price wearing a current timestamp, and the
        # gate would pass it. An unentitled session is exactly the case that
        # produces close-only payloads, which is exactly when a fabricated
        # "fresh" UMC price would feed a real spread and a real entry.
        if price is None:
            raise IbkrWorkerError(
                "IBKR UMC quote has no live price (last/bid/ask all absent); "
                f"tier={tier} ({market_data_tier_label(tier)}). "
                f"payload={payload}"
            )

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

    def fetch_ohlcv_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """1m RTH closes for the warmup window, as one historical request.

        A single IBKR chunk covers up to two months, and a warmup window is a
        handful of sessions, so no backwards walk is needed. Longer ranges are
        refused rather than silently truncated -- backfills belong to the
        dedicated historical module with its pacing rules.
        """
        if symbol.upper() != "UMC":
            raise ValueError(f"IBKR UMC provider does not serve symbol {symbol!r}")
        start_tpe = ensure_taipei(start)
        end_tpe = ensure_taipei(end)
        if start_tpe > end_tpe:
            raise ValueError("start must not be after end")
        span_days = (end_tpe - start_tpe).days + 2
        if span_days > 60:
            raise ValueError(
                f"fetch_ohlcv_1m spans {span_days} days; use "
                "integrations.ibkr.historical for backfills"
            )
        # IBKR's endDateTime bounds bars by their CLOSE: a 1m bar labelled T
        # spans T..T+1min, so requesting end=T would exclude the bar labelled T
        # itself. Ask one minute past the window and filter on labels below.
        rows = self.client.fetch_umc_historical_1m(
            end_date_time=_format_end(end_tpe + timedelta(minutes=1)),
            duration=f"{span_days} D",
            use_rth=True,
        )
        frame = bars_to_frame(rows)
        if frame.empty:
            return frame[["timestamp", "close"]]
        mask = (frame["timestamp"] >= start_tpe) & (frame["timestamp"] <= end_tpe)
        return frame.loc[mask, ["timestamp", "close"]].reset_index(drop=True)

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
