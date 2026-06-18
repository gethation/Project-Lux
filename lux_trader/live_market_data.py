from __future__ import annotations

import io
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from threading import Condition
from typing import Any, Callable, Protocol
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from zipfile import ZipFile

import pandas as pd

from .calendar import annotate_live_bar, in_night_session
from .config import LiveMarketDataConfig
from .models import MarketBar


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TIMEFRAME = "1m"
ONE_MINUTE_MS = 60_000
QFF_FORWARD_FILL_LOOKBACK = timedelta(days=7)
TAIFEX_PREVIOUS_30_URL = (
    "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData"
)
TAIFEX_DAILY_CSV_LINK_PATTERN = re.compile(
    r"(?P<url>(?:https?://www\.taifex\.com\.tw)?"
    r"/file/taifex/Dailydownload/DailydownloadCSV/"
    r"Daily_(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2})\.zip)"
)
QFF_MONTH_CODES = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "I": 9,
    "J": 10,
    "K": 11,
    "L": 12,
}


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


@dataclass(frozen=True)
class LiveQuoteSet:
    qff: LiveQuote
    tsm: LiveQuote
    usdttwd: LiveQuote


@dataclass(frozen=True)
class MinuteBuildResult:
    bar: MarketBar | None
    skipped_reason: str | None = None
    payload: dict[str, Any] | None = None
    quote_set: LiveQuoteSet | None = None


@dataclass(frozen=True)
class QffContractCandidate:
    symbol: str
    expiry: date
    raw: dict[str, Any]


@dataclass(frozen=True)
class TaifexDownloadEntry:
    trading_date: date
    csv_url: str


@dataclass(frozen=True)
class QffWarmupSourceReport:
    frame: pd.DataFrame
    start: datetime
    end: datetime
    qff_fetch_start: datetime
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
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        ...


class QffWarmupProvider(Protocol):
    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        ...


def floor_minute(timestamp: datetime) -> datetime:
    timestamp = ensure_taipei(timestamp)
    return timestamp.replace(second=0, microsecond=0)


def ensure_taipei(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=TAIPEI_TZ)
    return timestamp.astimezone(TAIPEI_TZ)


def parse_timestamp(value: Any) -> datetime:
    if value is None:
        return datetime.now(TAIPEI_TZ)
    if isinstance(value, datetime):
        return ensure_taipei(value)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000_000_000:
            return datetime.fromtimestamp(raw / 1_000_000_000, tz=TAIPEI_TZ)
        if raw > 10_000_000_000_000:
            return datetime.fromtimestamp(raw / 1_000_000, tz=TAIPEI_TZ)
        if raw > 10_000_000_000:
            return datetime.fromtimestamp(raw / 1000, tz=TAIPEI_TZ)
        return datetime.fromtimestamp(raw, tz=TAIPEI_TZ)
    text = str(value).strip()
    if not text:
        return datetime.now(TAIPEI_TZ)
    return ensure_taipei(pd.Timestamp(text).to_pydatetime())


def parse_optional_float(value: Any) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return float(parsed)


def first_book_level(levels: Any) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    if isinstance(levels, dict):
        level = levels
    else:
        level = levels[0]
    if isinstance(level, dict):
        return (
            parse_optional_float(row_get(level, "price", "px")),
            parse_optional_float(row_get(level, "size", "amount", "qty", "quantity")),
        )
    if isinstance(level, (list, tuple)):
        price = parse_optional_float(level[0]) if len(level) >= 1 else None
        size = parse_optional_float(level[1]) if len(level) >= 2 else None
        return price, size
    return None, None


def midpoint_or_single_side(
    bid: float | None,
    ask: float | None,
) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


def third_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_wednesday_offset = (2 - first.weekday()) % 7
    return first + timedelta(days=first_wednesday_offset + 14)


def row_get(row: Any, *names: str) -> Any:
    if isinstance(row, dict):
        for name in names:
            if name in row:
                return row[name]
            lowered = name.lower()
            for key, value in row.items():
                if str(key).lower() == lowered:
                    return value
        return None
    for name in names:
        if hasattr(row, name):
            return getattr(row, name)
    return None


def row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "__dict__"):
        return dict(row.__dict__)
    return {"value": str(row)}


def parse_contract_expiry(raw: dict[str, Any], product: str) -> date | None:
    for key in (
        "expiry",
        "expirationDate",
        "endDate",
        "settlementDate",
        "lastTradeDate",
        "deliveryDate",
        "maturityDate",
    ):
        value = row_get(raw, key)
        if value:
            try:
                return pd.Timestamp(str(value)).date()
            except Exception:
                pass

    for key in ("contract_month", "contractMonth", "deliveryMonth", "maturityMonth"):
        value = row_get(raw, key)
        if value:
            match = re.search(r"(20\d{2})(0[1-9]|1[0-2])", str(value))
            if match:
                return third_wednesday(int(match.group(1)), int(match.group(2)))

    symbol = str(row_get(raw, "symbol", "code", "id", "ticker") or "")
    match = re.search(rf"{re.escape(product)}.*?(20\d{{2}})(0[1-9]|1[0-2])", symbol)
    if match:
        return third_wednesday(int(match.group(1)), int(match.group(2)))
    return None


def qff_symbol_to_taifex_contract_month(
    symbol: str,
    *,
    reference_date: date | None = None,
) -> str:
    normalized = symbol.strip().upper()
    numeric = re.search(r"(20\d{2})(0[1-9]|1[0-2])", normalized)
    if numeric:
        return f"{numeric.group(1)}{numeric.group(2)}"

    coded = re.search(r"QFF([A-L])(\d)", normalized)
    if coded is None:
        raise RuntimeError(f"Cannot derive TAIFEX contract month from QFF symbol: {symbol}")

    reference = reference_date or datetime.now(TAIPEI_TZ).date()
    year_digit = int(coded.group(2))
    decade = reference.year - reference.year % 10
    year = decade + year_digit
    while year < reference.year - 1:
        year += 10
    month = QFF_MONTH_CODES[coded.group(1)]
    return f"{year}{month:02d}"


def select_qff_front_month(
    candidates: list[Any],
    *,
    product: str = "QFF",
    today: date | None = None,
) -> QffContractCandidate:
    today = today or datetime.now(TAIPEI_TZ).date()
    parsed: list[QffContractCandidate] = []
    rejected: list[str] = []

    for row in candidates:
        raw = row_to_dict(row)
        symbol = str(row_get(raw, "symbol", "code", "id", "ticker") or "").strip()
        if not symbol:
            rejected.append(str(raw))
            continue
        product_value = str(row_get(raw, "product", "productCode", "name") or symbol)
        if product.upper() not in product_value.upper() and product.upper() not in symbol.upper():
            continue
        expiry = parse_contract_expiry(raw, product)
        if expiry is None:
            rejected.append(symbol)
            continue
        if expiry >= today:
            parsed.append(QffContractCandidate(symbol=symbol, expiry=expiry, raw=raw))

    if not parsed:
        raise RuntimeError(
            "Unable to select QFF front-month contract. "
            f"Rejected candidates: {rejected[:10]}"
        )
    return sorted(parsed, key=lambda item: (item.expiry, item.symbol))[0]


class LiveMinuteBarBuilder:
    def __init__(
        self,
        *,
        stale_seconds: float,
        max_leg_timestamp_skew_seconds: float,
    ) -> None:
        self.stale_seconds = stale_seconds
        self.max_leg_timestamp_skew_seconds = max_leg_timestamp_skew_seconds
        self.current_minute: datetime | None = None
        self.current_quotes: dict[str, LiveQuote] = {}
        self.last_qff_close: float | None = None

    def update(
        self, quote_set: LiveQuoteSet, observed_at: datetime
    ) -> MinuteBuildResult | None:
        observed_at = ensure_taipei(observed_at)
        minute = floor_minute(observed_at)
        if self.current_minute is None:
            self.current_minute = minute
            self._update_current_quotes(quote_set)
            return None

        if minute == self.current_minute:
            self._update_current_quotes(quote_set)
            return None

        result = self._finalize_current_minute()
        self.current_minute = minute
        self.current_quotes = {}
        self._update_current_quotes(quote_set)
        return result

    def _update_current_quotes(self, quote_set: LiveQuoteSet) -> None:
        self.current_quotes["qff"] = quote_set.qff
        self.current_quotes["tsm"] = quote_set.tsm
        self.current_quotes["usdttwd"] = quote_set.usdttwd

    def _finalize_current_minute(self) -> MinuteBuildResult:
        if self.current_minute is None:
            return MinuteBuildResult(None, "no_current_minute")

        tsm = self.current_quotes.get("tsm")
        usdttwd = self.current_quotes.get("usdttwd")
        qff = self.current_quotes.get("qff")
        if tsm is None or usdttwd is None:
            return MinuteBuildResult(
                None,
                "missing_required_quote",
                {"minute": self.current_minute.isoformat()},
            )
        quote_set = LiveQuoteSet(qff=qff, tsm=tsm, usdttwd=usdttwd) if qff else None

        close_time = self.current_minute + timedelta(minutes=1)
        for name, quote in (("tsm", tsm), ("usdttwd", usdttwd)):
            age = abs((close_time - ensure_taipei(quote.timestamp)).total_seconds())
            if age > self.stale_seconds:
                return MinuteBuildResult(
                    None,
                    "market_data_stale",
                    {"source": name, "age_seconds": age},
                    quote_set,
                )

        qff_is_fresh = False
        if qff is not None:
            qff_age = abs((close_time - ensure_taipei(qff.timestamp)).total_seconds())
            qff_is_fresh = qff_age <= self.stale_seconds

        skew_quotes = [tsm, usdttwd]
        if qff is not None and qff_is_fresh:
            skew_quotes.append(qff)
        timestamps = [ensure_taipei(quote.timestamp) for quote in skew_quotes]
        skew = (max(timestamps) - min(timestamps)).total_seconds()
        if skew > self.max_leg_timestamp_skew_seconds:
            return MinuteBuildResult(
                None,
                "leg_timestamp_skew",
                {"skew_seconds": skew},
                quote_set,
            )

        qff_close = qff.price if qff is not None and qff_is_fresh else None
        if qff_close is not None:
            self.last_qff_close = qff_close
        if self.last_qff_close is None:
            return MinuteBuildResult(
                None,
                "missing_qff_forward_fill",
                quote_set=quote_set,
            )

        tsm_twd_fair = tsm.price * usdttwd.price / 5.0
        spread = (
            (tsm_twd_fair - self.last_qff_close)
            / (tsm_twd_fair + self.last_qff_close)
            * 200.0
        )
        return MinuteBuildResult(
            annotate_live_bar(
                MarketBar(
                    row_index=-1,
                    timestamp=self.current_minute,
                    qff_close=qff_close,
                    qff_close_filled=self.last_qff_close,
                    tsm_twd_fair=tsm_twd_fair,
                    spread=spread,
                )
            ),
            quote_set=quote_set,
        )


class CcxtTickerMarketData:
    def __init__(self, exchange_id: str, timeout_ms: int = 30_000) -> None:
        import ccxt

        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class({"enableRateLimit": True, "timeout": timeout_ms})
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
            if "not valid depth limit" in book_error or '"code":-4021' in book_error:
                book_limit_used = 5
                fetched_book = self.exchange.fetch_order_book(symbol, limit=5)
                order_book = dict(fetched_book or {})
                book_error = None

        bid, bid_size = first_book_level(order_book.get("bids"))
        ask, ask_size = first_book_level(order_book.get("asks"))
        price = midpoint_or_single_side(bid, ask)
        if price is None:
            ticker = self.exchange.fetch_ticker(symbol)
            price = parse_optional_float(ticker.get("last")) or parse_optional_float(
                ticker.get("close")
            )
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
        self, symbol: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        start_utc = ensure_taipei(start).astimezone(ZoneInfo("UTC"))
        end_utc = ensure_taipei(end).astimezone(ZoneInfo("UTC"))
        since_ms = int(start_utc.timestamp() * 1000)
        end_ms = int(end_utc.timestamp() * 1000)
        rows: list[list[float]] = []

        while since_ms <= end_ms:
            batch = self.exchange.fetch_ohlcv(symbol, TIMEFRAME, since=since_ms, limit=1000)
            if not batch:
                break
            rows.extend(batch)
            last_ts = int(batch[-1][0])
            if last_ts < since_ms:
                raise RuntimeError(f"{self.exchange_id} returned non-advancing OHLCV")
            since_ms = last_ts + ONE_MINUTE_MS
            time.sleep(float(getattr(self.exchange, "rateLimit", 0)) / 1000.0)

        return normalize_ohlcv_rows(rows, start, end)


class TaifexQffTradeDownloader:
    def __init__(
        self,
        cache_dir: Path,
        *,
        page_url: str = TAIFEX_PREVIOUS_30_URL,
        timeout_seconds: float = 30.0,
        http_get: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.page_url = page_url
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get or self._http_get

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        start = ensure_taipei(start)
        end = ensure_taipei(end)
        contract_month = qff_symbol_to_taifex_contract_month(
            symbol,
            reference_date=end.date(),
        )
        entries = self.entries_by_date()
        frames: list[pd.DataFrame] = []
        for trading_date in calendar_dates(start.date(), end.date() + timedelta(days=1)):
            entry = entries.get(trading_date)
            if entry is None:
                continue
            frames.append(self._read_qff_1m(entry, contract_month))

        if not frames:
            return pd.DataFrame(columns=["timestamp", "close"])
        frame = pd.concat(frames, ignore_index=True)
        if frame.empty:
            return pd.DataFrame(columns=["timestamp", "close"])
        frame = frame.sort_values("timestamp")
        frame = frame.drop_duplicates("timestamp", keep="last")
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        return frame.loc[
            (frame["timestamp"] >= start_ts) & (frame["timestamp"] <= end_ts),
            ["timestamp", "close"],
        ].copy()

    def entries_by_date(self) -> dict[date, TaifexDownloadEntry]:
        html = self.http_get(self.page_url).decode("utf-8", errors="replace")
        entries = parse_taifex_download_entries(html, base_url=self.page_url)
        return {entry.trading_date: entry for entry in entries}

    def _read_qff_1m(
        self,
        entry: TaifexDownloadEntry,
        contract_month: str,
    ) -> pd.DataFrame:
        archive = self._download_zip(entry)
        rows: list[pd.DataFrame] = []
        with ZipFile(io.BytesIO(archive)) as zip_file:
            csv_names = [
                name for name in zip_file.namelist() if name.lower().endswith(".csv")
            ]
            if not csv_names:
                raise RuntimeError(f"TAIFEX ZIP has no CSV: {entry.csv_url}")
            for csv_name in csv_names:
                with zip_file.open(csv_name) as csv_file:
                    rows.extend(parse_taifex_qff_tick_csv(csv_file, contract_month))

        if not rows:
            return pd.DataFrame(columns=["timestamp", "close"])
        frame = pd.concat(rows, ignore_index=True)
        frame = frame.sort_values("timestamp")
        return (
            frame.groupby("timestamp", sort=True, as_index=False)
            .last()[["timestamp", "close"]]
            .copy()
        )

    def _download_zip(self, entry: TaifexDownloadEntry) -> bytes:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.cache_dir / f"Daily_{entry.trading_date:%Y_%m_%d}.zip"
        if cache_path.exists() and cache_path.stat().st_size > 0:
            return cache_path.read_bytes()

        payload = self.http_get(entry.csv_url)
        if not payload.startswith(b"PK"):
            raise RuntimeError(f"TAIFEX download is not a ZIP file: {entry.csv_url}")
        cache_path.write_bytes(payload)
        return payload

    def _http_get(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "ProjectLux/0.1 (+https://www.taifex.com.tw)",
                "Accept": "text/html,application/zip,*/*",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            return response.read()


def parse_taifex_download_entries(
    html: str,
    *,
    base_url: str = TAIFEX_PREVIOUS_30_URL,
) -> list[TaifexDownloadEntry]:
    entries: dict[date, TaifexDownloadEntry] = {}
    for match in TAIFEX_DAILY_CSV_LINK_PATTERN.finditer(unescape(html)):
        trading_date = date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
        entries[trading_date] = TaifexDownloadEntry(
            trading_date=trading_date,
            csv_url=urljoin(base_url, match.group("url")),
        )
    if not entries:
        raise RuntimeError("Unable to parse TAIFEX daily CSV download links")
    return sorted(entries.values(), key=lambda item: item.trading_date)


def parse_taifex_qff_tick_csv(
    csv_file: Any,
    contract_month: str,
) -> list[pd.DataFrame]:
    chunks: list[pd.DataFrame] = []
    for chunk in pd.read_csv(
        csv_file,
        encoding="cp950",
        dtype=str,
        chunksize=200_000,
    ):
        chunk = chunk.rename(columns=lambda column: str(column).strip())
        required = {
            "成交日期",
            "商品代號",
            "到期月份(週別)",
            "成交時間",
            "成交價格",
        }
        missing = required.difference(chunk.columns)
        if missing:
            raise RuntimeError(f"TAIFEX tick CSV missing columns: {sorted(missing)}")

        filtered = chunk.loc[
            (chunk["商品代號"].astype(str).str.strip() == "QFF")
            & (chunk["到期月份(週別)"].astype(str).str.strip() == contract_month)
        ].copy()
        if filtered.empty:
            continue

        date_text = filtered["成交日期"].astype(str).str.strip()
        time_text = filtered["成交時間"].astype(str).str.strip().str.zfill(6)
        timestamps = pd.to_datetime(
            date_text + time_text,
            format="%Y%m%d%H%M%S",
            errors="coerce",
        ).dt.tz_localize(TAIPEI_TZ)
        prices = pd.to_numeric(filtered["成交價格"], errors="coerce")
        parsed = pd.DataFrame(
            {
                "timestamp": timestamps.dt.floor("min"),
                "close": prices,
            }
        ).dropna()
        if not parsed.empty:
            chunks.append(parsed)
    return chunks


class CsvQffWarmupProvider:
    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if not self.path.exists():
            raise FileNotFoundError(f"TAIFEX QFF CSV does not exist: {self.path}")
        frame = pd.read_csv(self.path)
        required = {"timestamp", "close"}
        missing = required.difference(frame.columns)
        if missing:
            raise RuntimeError(f"{self.path} missing columns: {sorted(missing)}")
        frame = frame.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
            TAIPEI_TZ
        )
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        start_ts = pd.Timestamp(ensure_taipei(start))
        end_ts = pd.Timestamp(ensure_taipei(end))
        return frame.loc[
            (frame["timestamp"] >= start_ts) & (frame["timestamp"] <= end_ts),
            ["timestamp", "close"],
        ].copy()


def candidate_rows(data: Any) -> list[Any]:
    if data is None:
        return []
    if isinstance(data, dict):
        for key in ("data", "items", "tickers", "contracts", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return list(value)
        return [data] if data else []
    if isinstance(data, (str, bytes)):
        return []
    try:
        return list(data)
    except TypeError:
        return [data]


def dedupe_candidates(candidates: list[Any]) -> list[Any]:
    seen: set[str] = set()
    unique: list[Any] = []
    for candidate in candidates:
        raw = row_to_dict(candidate)
        symbol = str(row_get(raw, "symbol", "code", "id", "ticker") or "").strip()
        key = symbol or repr(raw)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def summarize_candidate_response(result: Any, rows: list[Any]) -> str:
    result_type = type(result).__name__
    if isinstance(result, dict):
        keys = sorted(str(key) for key in result.keys())[:8]
        status = row_get(result, "status", "code", "message")
        sample = summarize_candidate_row(rows[0]) if rows else "none"
        return (
            f"type=dict keys={keys} status={status!r} "
            f"count={len(rows)} sample={sample}"
        )
    sample = summarize_candidate_row(rows[0]) if rows else "none"
    return f"type={result_type} count={len(rows)} sample={sample}"


def summarize_candidate_row(row: Any) -> str:
    raw = row_to_dict(row)
    compact = {
        str(key): raw[key]
        for key in list(raw.keys())[:8]
        if key in raw
    }
    text = repr(compact)
    if len(text) > 400:
        return text[:397] + "..."
    return text


class FubonQffMarketData:
    def __init__(
        self,
        env_path: Path | None = None,
        *,
        book_wait_timeout_seconds: float = 5.0,
    ) -> None:
        self.env_path = env_path
        self.sdk = None
        self.intraday = None
        self.websocket = None
        self.book_wait_timeout_seconds = book_wait_timeout_seconds
        self._book_condition = Condition()
        self._latest_books: dict[str, LiveQuote] = {}
        self._book_subscription_ids: dict[str, str] = {}
        self._book_subscribed_symbols: set[str] = set()
        self._websocket_connected = False
        self._websocket_handlers_registered = False
        self.last_candidate_session_counts: dict[str, int] = {}
        self.last_candidate_session_summaries: dict[str, str] = {}

    def connect(self) -> None:
        from fubon_neo.sdk import FubonSDK, Mode

        load_dotenv(self.env_path)
        personal_id = require_env("FUBON_PERSONAL_ID")
        cert_path = resolve_cert_path(self.env_path)
        cert_password = os.getenv("FUBON_CERT_PASSWORD", "").strip() or None
        api_key = os.getenv("FUBON_API_KEY", "").strip()
        password = os.getenv("FUBON_PASSWORD", "").strip()

        sdk = FubonSDK()
        if api_key:
            result = sdk.apikey_login(personal_id, api_key, str(cert_path), cert_password)
        elif password:
            if cert_password:
                result = sdk.login(personal_id, password, str(cert_path), cert_password)
            else:
                result = sdk.login(personal_id, password, str(cert_path))
        else:
            raise RuntimeError("Set FUBON_API_KEY or FUBON_PASSWORD for market data login")

        if not bool(getattr(result, "is_success", False)):
            raise RuntimeError(f"Fubon login failed: {getattr(result, 'message', '')}")
        mode = getattr(Mode, "Normal", None)
        if mode is None:
            sdk.init_realtime()
        else:
            sdk.init_realtime(mode)
        self.sdk = sdk
        self.intraday = sdk.marketdata.rest_client.futopt.intraday
        self.websocket = sdk.marketdata.websocket_client.futopt

    def close(self) -> None:
        if self.websocket is not None:
            try:
                self.websocket.disconnect()
            except Exception:
                pass
        if self.sdk is not None:
            self.sdk.logout()

    def _require_intraday(self) -> Any:
        if self.intraday is None:
            self.connect()
        return self.intraday

    def fetch_candidates(self, product: str) -> list[Any]:
        intraday = self._require_intraday()
        candidates: list[Any] = []
        errors: dict[str, str] = {}
        counts: dict[str, int] = {}
        summaries: dict[str, str] = {}
        for session in ("REGULAR", "AFTERHOURS"):
            try:
                result = intraday.tickers(
                    type="FUTURE", exchange="TAIFEX", session=session, product=product
                )
                data = result.get("data", result) if isinstance(result, dict) else result
                rows = candidate_rows(data)
                counts[session] = len(rows)
                summaries[session] = summarize_candidate_response(result, rows)
                candidates.extend(rows)
            except Exception as exc:
                counts[session] = 0
                errors[session] = str(exc)
                summaries[session] = f"error={exc}"
        self.last_candidate_session_counts = counts
        self.last_candidate_session_summaries = summaries
        if candidates:
            return dedupe_candidates(candidates)
        raise RuntimeError(
            "Fubon QFF ticker lookup returned no candidates. "
            f"session_counts={counts}; session_summaries={summaries}; errors={errors}"
        )

    def select_front_month_symbol(self, product: str) -> str:
        return select_qff_front_month(self.fetch_candidates(product), product=product).symbol

    def fetch_quote(self, symbol: str) -> LiveQuote:
        try:
            self.ensure_books_subscription(symbol)
        except Exception:
            pass
        quote = self._wait_for_book_quote(symbol)
        if quote is not None:
            return quote
        return self._fetch_rest_quote_for_diagnostics(symbol)

    def ensure_books_subscription(
        self,
        symbol: str,
        *,
        after_hours: bool | None = None,
    ) -> None:
        self._require_intraday()
        if self.websocket is None:
            raise RuntimeError("Fubon futopt websocket client is not available")
        with self._book_condition:
            if symbol in self._book_subscribed_symbols:
                return
        self._ensure_websocket_connected()
        params: dict[str, Any] = {
            "channel": "books",
            "symbol": symbol,
            "afterHours": self._after_hours_now() if after_hours is None else after_hours,
        }
        self.websocket.subscribe(params)
        with self._book_condition:
            self._book_subscribed_symbols.add(symbol)

    def unsubscribe_books(self, symbol: str) -> None:
        if self.websocket is None:
            return
        with self._book_condition:
            if symbol not in self._book_subscribed_symbols:
                return
            subscription_id = self._book_subscription_ids.pop(symbol, None)
            self._book_subscribed_symbols.discard(symbol)
            self._latest_books.pop(symbol, None)
        try:
            if subscription_id:
                self.websocket.unsubscribe({"id": subscription_id})
            else:
                self.websocket.unsubscribe({"channel": "books", "symbol": symbol})
        except Exception:
            pass

    def _ensure_websocket_connected(self) -> None:
        if self.websocket is None:
            raise RuntimeError("Fubon futopt websocket client is not available")
        if not self._websocket_handlers_registered:
            self.websocket.on("message", self._handle_websocket_message)
            self.websocket.on("error", self._handle_websocket_error)
            self._websocket_handlers_registered = True
        if not self._websocket_connected:
            self.websocket.connect()
            self._websocket_connected = True

    def _handle_websocket_error(self, error: Any) -> None:
        with self._book_condition:
            self._book_condition.notify_all()

    def _handle_websocket_message(self, raw_message: Any) -> None:
        try:
            message = decode_websocket_message(raw_message)
        except Exception:
            return
        if not isinstance(message, dict):
            return
        self._remember_book_subscription(message)
        quote = parse_fubon_books_quote(message)
        if quote is None:
            return
        with self._book_condition:
            self._latest_books[quote.symbol] = quote
            self._book_condition.notify_all()

    def _remember_book_subscription(self, message: dict[str, Any]) -> None:
        event = str(row_get(message, "event") or "").lower()
        if event not in {"subscribed", "subscribed_books"}:
            return
        data = row_get(message, "data")
        rows = data if isinstance(data, list) else [data or message]
        with self._book_condition:
            for row in rows:
                if row is None:
                    continue
                symbol = row_get(row, "symbol")
                subscription_id = row_get(row, "id")
                channel = str(row_get(row, "channel") or row_get(message, "channel") or "")
                if symbol and subscription_id and channel.lower() == "books":
                    self._book_subscription_ids[str(symbol)] = str(subscription_id)

    def _wait_for_book_quote(self, symbol: str) -> LiveQuote | None:
        deadline = time.monotonic() + self.book_wait_timeout_seconds
        with self._book_condition:
            quote = self._latest_books.get(symbol)
            while quote is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._book_condition.wait(remaining)
                quote = self._latest_books.get(symbol)
            return quote

    def _fetch_rest_quote_for_diagnostics(self, symbol: str) -> LiveQuote:
        intraday = self._require_intraday()
        raw = self._fetch_intraday_quote(intraday, symbol)
        payload = raw.get("data", raw) if isinstance(raw, dict) else row_to_dict(raw)
        price = first_float(
            payload,
            "closePrice",
            "lastPrice",
            "price",
            "close",
            "last",
            "referencePrice",
        )
        if price is None:
            raise RuntimeError(f"Fubon quote has no usable price: {payload}")
        last_trade = row_to_dict(row_get(payload, "lastTrade") or {})
        return LiveQuote(
            source="fubon_qff",
            symbol=symbol,
            timestamp=parse_timestamp(
                row_get(payload, "dateTime", "time", "timestamp", "lastUpdated")
            ),
            price=price,
            bid=None,
            ask=None,
            raw={
                "rest_quote": payload,
                "rest_last_trade_bid": first_float(last_trade, "bid"),
                "rest_last_trade_ask": first_float(last_trade, "ask"),
                "book_missing": True,
            },
        )

    def _fetch_intraday_quote(self, intraday: Any, symbol: str) -> Any:
        if self._after_hours_now():
            try:
                return intraday.quote(symbol=symbol, session="afterhours")
            except TypeError:
                pass
            except Exception:
                pass
        return intraday.quote(symbol=symbol)

    def _after_hours_now(self) -> bool:
        return in_night_session(datetime.now(TAIPEI_TZ))

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        intraday = self._require_intraday()
        raw = intraday.candles(symbol=symbol, timeframe="1")
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        return normalize_candle_rows(list(data or []), start, end)


class WarmupBuilder:
    def __init__(
        self,
        *,
        live_config: LiveMarketDataConfig,
        qff_intraday_provider: QffWarmupProvider | None,
        qff_fallback_provider: QffWarmupProvider | None,
        tsm_provider: OhlcvProvider,
        usdttwd_provider: OhlcvProvider,
    ) -> None:
        self.live_config = live_config
        self.qff_intraday_provider = qff_intraday_provider
        self.qff_fallback_provider = qff_fallback_provider
        self.tsm_provider = tsm_provider
        self.usdttwd_provider = usdttwd_provider

    def build(
        self,
        *,
        qff_symbol: str,
        qff_expiry: str | None = None,
        contract_policy_state: str | None = None,
        end: datetime | None = None,
    ) -> list[MarketBar]:
        end_minute = floor_minute(end or datetime.now(TAIPEI_TZ)) - timedelta(minutes=1)
        start_minute = end_minute - timedelta(minutes=self.live_config.warmup_minutes - 1)
        index = pd.date_range(start_minute, end_minute, freq="min")
        qff_fetch_start = start_minute - QFF_FORWARD_FILL_LOOKBACK

        qff_parts: list[tuple[str, pd.DataFrame]] = []
        if self.qff_fallback_provider is not None:
            qff_parts.append(
                (
                    "fallback",
                    self.qff_fallback_provider.fetch_1m(
                        qff_symbol, qff_fetch_start, end_minute
                    ),
                )
            )
        if self.qff_intraday_provider is not None:
            qff_parts.append(
                (
                    "intraday",
                    self.qff_intraday_provider.fetch_1m(
                        qff_symbol, qff_fetch_start, end_minute
                    ),
                )
            )
        if not qff_parts:
            raise RuntimeError("No QFF warmup providers configured")
        qff_report = build_qff_warmup_source_report(
            qff_parts,
            start_minute=start_minute,
            end_minute=end_minute,
            qff_fetch_start=qff_fetch_start,
        )
        if qff_report.null_count:
            first_missing = qff_report.frame.loc[
                qff_report.frame["qff_close_filled"].isna(), "timestamp"
            ].iloc[0]
            raise RuntimeError(f"QFF warmup cannot forward-fill from {first_missing}")
        qff = pd.Series(
            qff_report.frame["merged_qff_close"].to_numpy(),
            index=pd.DatetimeIndex(qff_report.frame["timestamp"]),
        )
        qff_filled = pd.Series(
            qff_report.frame["qff_close_filled"].to_numpy(),
            index=pd.DatetimeIndex(qff_report.frame["timestamp"]),
        )

        tsm = close_series(
            self.tsm_provider.fetch_ohlcv_1m(
                self.live_config.binance_symbol, start_minute, end_minute
            ),
            "tsm",
        ).reindex(index)
        usd = close_series(
            self.usdttwd_provider.fetch_ohlcv_1m(
                self.live_config.bitopro_symbol, start_minute, end_minute
            ),
            "usdttwd",
        ).reindex(index)
        missing = tsm[tsm.isna()].index.union(usd[usd.isna()].index)
        if len(missing):
            raise RuntimeError(f"TSM/USDT-TWD warmup has missing minutes from {missing[0]}")

        tsm_twd_fair = tsm * usd / 5.0
        spread = (tsm_twd_fair - qff_filled) / (tsm_twd_fair + qff_filled) * 200.0
        bars: list[MarketBar] = []
        for row_index, timestamp in enumerate(index):
            qff_close = parse_optional_float(qff.loc[timestamp])
            bars.append(
                MarketBar(
                    row_index=row_index - len(index),
                    timestamp=timestamp.to_pydatetime(),
                    qff_close=qff_close,
                    qff_close_filled=float(qff_filled.loc[timestamp]),
                    tsm_twd_fair=float(tsm_twd_fair.loc[timestamp]),
                    spread=float(spread.loc[timestamp]),
                    qff_symbol=qff_symbol,
                    qff_expiry=qff_expiry,
                    contract_policy_state=contract_policy_state,
                )
            )
        return bars


def build_qff_warmup_source_report(
    frames: list[tuple[str, pd.DataFrame]],
    *,
    start_minute: datetime,
    end_minute: datetime,
    qff_fetch_start: datetime,
) -> QffWarmupSourceReport:
    start_minute = floor_minute(start_minute)
    end_minute = floor_minute(end_minute)
    qff_fetch_start = floor_minute(qff_fetch_start)
    warmup_index = pd.date_range(start_minute, end_minute, freq="min")
    fill_index = pd.date_range(qff_fetch_start, end_minute, freq="min")

    source_series: dict[str, pd.Series] = {}
    combined_parts: list[pd.DataFrame] = []
    for priority, (source, frame) in enumerate(frames):
        series = close_series(frame, source)
        source_series[source] = series
        if series.empty:
            continue
        combined_parts.append(
            pd.DataFrame(
                {
                    "timestamp": series.index,
                    "close": series.to_numpy(),
                    "source": source,
                    "priority": priority,
                }
            )
        )

    if combined_parts:
        combined = pd.concat(combined_parts, ignore_index=True).sort_values(
            ["timestamp", "priority"]
        )
        combined = combined.drop_duplicates("timestamp", keep="last").set_index(
            "timestamp"
        )
    else:
        combined = pd.DataFrame(columns=["close", "source", "priority"])
        combined.index = pd.DatetimeIndex([], tz=TAIPEI_TZ)

    report = pd.DataFrame({"timestamp": warmup_index})
    for source, series in source_series.items():
        report[f"{source}_close"] = series.reindex(warmup_index).to_numpy()
    report["merged_qff_close"] = combined["close"].reindex(warmup_index).to_numpy()
    filled = combined["close"].reindex(fill_index).ffill().reindex(warmup_index)
    direct_source = combined["source"].reindex(warmup_index)
    report["qff_close_filled"] = filled.to_numpy()
    report["source_used"] = direct_source.where(
        direct_source.notna(),
        other=pd.Series("forward_fill", index=warmup_index).where(filled.notna()),
    ).to_numpy()

    overlap_rows = 0
    mismatch_count = 0
    max_abs_diff = 0.0
    if "taifex" in source_series and "fubon" in source_series:
        overlap = pd.DataFrame(
            {
                "taifex": source_series["taifex"],
                "fubon": source_series["fubon"],
            }
        ).dropna()
        overlap_rows = len(overlap)
        if overlap_rows:
            diffs = (overlap["taifex"] - overlap["fubon"]).abs()
            mismatches = diffs[diffs > 1e-9]
            mismatch_count = len(mismatches)
            max_abs_diff = float(mismatches.max()) if mismatch_count else 0.0

    return QffWarmupSourceReport(
        frame=report,
        start=start_minute,
        end=end_minute,
        qff_fetch_start=qff_fetch_start,
        source_rows={source: len(series) for source, series in source_series.items()},
        source_used_counts={
            str(key): int(value)
            for key, value in report["source_used"].value_counts(dropna=False).items()
        },
        null_count=int(report["qff_close_filled"].isna().sum()),
        overlap_rows=overlap_rows,
        mismatch_count=mismatch_count,
        max_abs_diff=max_abs_diff,
    )


def combine_close_frames(frames: list[pd.DataFrame]) -> pd.Series:
    parts = [close_series(frame, "qff") for frame in frames if not frame.empty]
    if not parts:
        return pd.Series(dtype=float, name="qff")
    combined = pd.concat(parts).sort_index()
    return combined[~combined.index.duplicated(keep="last")]


def close_series(frame: pd.DataFrame, name: str) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float, name=name)
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True).dt.tz_convert(
        TAIPEI_TZ
    )
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    if data["close"].isna().any():
        raise RuntimeError(f"{name} close series contains invalid values")
    series = pd.Series(
        data["close"].to_numpy(),
        index=pd.DatetimeIndex(data["timestamp"]),
        name=name,
    )
    return series.sort_index()


def normalize_ohlcv_rows(
    rows: list[list[float]], start: datetime, end: datetime
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "close"])
    frame = pd.DataFrame(
        rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
    )
    frame = frame.drop_duplicates("timestamp_ms", keep="last").sort_values("timestamp_ms")
    frame["timestamp"] = pd.to_datetime(frame["timestamp_ms"], unit="ms", utc=True).dt.tz_convert(TAIPEI_TZ)
    start_ts = pd.Timestamp(ensure_taipei(start))
    end_ts = pd.Timestamp(ensure_taipei(end))
    return frame.loc[
        (frame["timestamp"] >= start_ts) & (frame["timestamp"] <= end_ts),
        ["timestamp", "close"],
    ].copy()


def normalize_candle_rows(
    rows: list[Any], start: datetime, end: datetime
) -> pd.DataFrame:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        raw = row_to_dict(row)
        timestamp = row_get(raw, "date", "time", "timestamp", "dateTime")
        close = first_float(raw, "close", "closePrice", "lastPrice", "price")
        if timestamp is None or close is None:
            continue
        normalized.append({"timestamp": parse_timestamp(timestamp), "close": close})
    frame = pd.DataFrame(normalized, columns=["timestamp", "close"])
    if frame.empty:
        return frame
    start_ts = pd.Timestamp(ensure_taipei(start))
    end_ts = pd.Timestamp(ensure_taipei(end))
    return frame.loc[
        (pd.DatetimeIndex(frame["timestamp"]) >= start_ts)
        & (pd.DatetimeIndex(frame["timestamp"]) <= end_ts)
    ].copy()


def decode_websocket_message(raw_message: Any) -> Any:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8", errors="replace")
    if isinstance(raw_message, str):
        return json.loads(raw_message)
    return raw_message


def parse_fubon_books_quote(message: dict[str, Any]) -> LiveQuote | None:
    channel = str(row_get(message, "channel") or "").lower()
    event = str(row_get(message, "event") or "").lower()
    data = row_get(message, "data")
    if data is None:
        data = message
    if isinstance(data, list):
        if not data:
            return None
        data = data[0]
    if not isinstance(data, dict):
        data = row_to_dict(data)
    data_channel = str(row_get(data, "channel") or channel).lower()
    if data_channel and data_channel != "books":
        return None
    if event and event not in {"data", "snapshot", "books"} and not row_get(data, "bids"):
        return None

    symbol = str(row_get(data, "symbol", "code", "id") or "").strip()
    if not symbol:
        return None
    bid, bid_size = first_book_level(row_get(data, "bids", "bid"))
    ask, ask_size = first_book_level(row_get(data, "asks", "ask"))
    price = midpoint_or_single_side(bid, ask)
    if price is None:
        return None
    timestamp = parse_timestamp(
        row_get(data, "time", "dateTime", "timestamp", "lastUpdated")
        or row_get(message, "time", "dateTime", "timestamp")
    )
    return LiveQuote(
        source="fubon_qff",
        symbol=symbol,
        timestamp=timestamp,
        price=price,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        raw={
            "books": data,
            "message": message,
            "bid_size": bid_size,
            "ask_size": ask_size,
        },
    )


def first_float(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        parsed = parse_optional_float(row_get(row, name))
        if parsed is not None:
            return parsed
    return None


def calendar_dates(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def load_dotenv(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def resolve_cert_path(env_path: Path | None) -> Path:
    value = os.getenv("FUBON_CERT_PATH", "").strip()
    root = env_path.parent if env_path is not None else Path.cwd()
    if value:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = root / path
        return path.resolve()
    candidates = sorted(root.glob("*.pfx"))
    if len(candidates) == 1:
        return candidates[0].resolve()
    raise RuntimeError("Set FUBON_CERT_PATH or place exactly one .pfx next to .env")
