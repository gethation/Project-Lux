from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import pandas as pd

from ..core.models import MarketBar


@dataclass(frozen=True)
class LiveQuote:
    source: str
    symbol: str
    timestamp: datetime
    price: float
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    raw: dict[str, Any] | None = None
    # IBKR serves a tier per subscription (1 live, 2 frozen, 3 delayed,
    # 4 delayed-frozen) and will happily downgrade without being asked. Carrying
    # it on the quote means the runtime can say what it is trading on rather
    # than assuming.
    market_data_tier: int | None = None
    is_delayed: bool = False


@dataclass(frozen=True)
class LiveQuoteSet:
    ccf: LiveQuote
    umc: LiveQuote
    usd_twd: LiveQuote


@dataclass(frozen=True)
class MinuteBuildResult:
    bar: MarketBar | None
    skipped_reason: str | None = None
    payload: dict[str, Any] | None = None
    quote_set: LiveQuoteSet | None = None


@dataclass(frozen=True)
class CcfContractCandidate:
    symbol: str
    expiry: date
    raw: dict[str, Any]


@dataclass(frozen=True)
class CcfWarmupSourceReport:
    frame: pd.DataFrame
    start: datetime
    end: datetime
    ccf_fetch_start: datetime
    source_rows: dict[str, int]
    source_used_counts: dict[str, int]
    null_count: int
    overlap_rows: int
    mismatch_count: int
    max_abs_diff: float


class QuoteProvider(Protocol):
    def fetch_quote(self, symbol: str) -> LiveQuote:
        ...


class OhlcvProvider(Protocol):
    def fetch_ohlcv_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        ...


class CcfWarmupProvider(Protocol):
    def fetch_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        ...

