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
    # When `price` was last TRUE, which is not always when the quote was last
    # updated. They are the same for CCF and FX. For UMC they are not: the quote
    # is stamped with the book clock, because that is what the directional
    # signal consumes, while `price` is the last TRADE -- and a liquid ADR still
    # goes tens of seconds without printing.
    #
    # Two different consumers need two different answers. The bar's close is a
    # trade price, so it must be bounded by THIS; the signal crosses the book,
    # so it is bounded by `timestamp`. Collapsing them into one field is how a
    # 30-second-old print came to be stamped fresh and laundered into the
    # rolling z-score. None means "no separate answer" -- treat as `timestamp`.
    price_timestamp: datetime | None = None
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

