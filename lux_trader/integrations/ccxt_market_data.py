from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from ..core.time import TAIPEI_TZ, ensure_taipei
from ..market_data.normalization import normalize_ohlcv_rows
from ..market_data.parsing import (
    first_book_level,
    midpoint_or_single_side,
    parse_optional_float,
    parse_timestamp,
)
from ..market_data.types import LiveQuote


TIMEFRAME = "1m"
ONE_MINUTE_MS = 60_000


class CcxtTickerMarketData:
    def __init__(self, exchange_id: str, timeout_ms: int = 30_000) -> None:
        import ccxt

        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class(
            {"enableRateLimit": True, "timeout": timeout_ms}
        )
        self.exchange.load_markets()
        self.exchange_id = exchange_id

    def fetch_quote(self, symbol: str) -> LiveQuote:
        observed_at = datetime.now(TAIPEI_TZ)
        order_book: dict[str, Any] = {}
        ticker: dict[str, Any] | None = None
        book_error: str | None = None
        book_limit_used = 1
        try:
            fetched_book = self.exchange.fetch_order_book(symbol, limit=1)
            order_book = dict(fetched_book or {})
        except Exception as exc:
            book_error = str(exc)
            if (
                "not valid depth limit" in book_error
                or '"code":-4021' in book_error
            ):
                book_limit_used = 5
                try:
                    fetched_book = self.exchange.fetch_order_book(
                        symbol,
                        limit=5,
                    )
                    order_book = dict(fetched_book or {})
                    book_error = None
                except Exception as retry_exc:
                    book_error = (
                        f"{book_error}; "
                        f"retry_limit_5:{type(retry_exc).__name__}"
                    )

        bid, bid_size = first_book_level(order_book.get("bids"))
        ask, ask_size = first_book_level(order_book.get("asks"))
        price = midpoint_or_single_side(bid, ask)
        if price is None:
            ticker = self.exchange.fetch_ticker(symbol)
            price = parse_optional_float(ticker.get("last"))
            if price is None:
                price = parse_optional_float(ticker.get("close"))
        if price is None:
            raise RuntimeError(
                f"{self.exchange_id} order book has no usable price: "
                f"order_book={order_book}, ticker={ticker}"
            )
        return LiveQuote(
            source=self.exchange_id,
            symbol=symbol,
            timestamp=(
                parse_timestamp(order_book.get("timestamp"))
                if order_book.get("timestamp") is not None
                else observed_at
            ),
            price=price,
            bid=bid,
            ask=ask,
            bid_size=bid_size,
            ask_size=ask_size,
            raw={
                "order_book": order_book,
                "ticker": ticker,
                "book_error": book_error,
                "book_limit_used": book_limit_used,
                "book_missing": bid is None or ask is None,
                "bid_size": bid_size,
                "ask_size": ask_size,
            },
        )

    def fetch_ohlcv_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        start_utc = ensure_taipei(start).astimezone(ZoneInfo("UTC"))
        end_utc = ensure_taipei(end).astimezone(ZoneInfo("UTC"))
        since_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        rows: list[list[float]] = []

        while since_ms <= end_ms:
            batch = self.exchange.fetch_ohlcv(
                symbol,
                TIMEFRAME,
                since=since_ms,
                limit=1000,
            )
            if not batch:
                break
            rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts < since_ms:
                raise RuntimeError(
                    f"{self.exchange_id} returned non-advancing OHLCV"
                )
            since_ms = last_ts + ONE_MINUTE_MS
            time.sleep(
                float(getattr(self.exchange, "rateLimit", 0)) / 1000.0
            )

        return normalize_ohlcv_rows(rows, start, end)

