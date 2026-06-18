from __future__ import annotations

import io
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from zipfile import ZipFile

import pandas as pd

from .calendar import annotate_live_bar
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

        close_time = self.current_minute + timedelta(minutes=1)
        for name, quote in (("tsm", tsm), ("usdttwd", usdttwd)):
            age = abs((close_time - ensure_taipei(quote.timestamp)).total_seconds())
            if age > self.stale_seconds:
                return MinuteBuildResult(
                    None,
                    "market_data_stale",
                    {"source": name, "age_seconds": age},
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
            return MinuteBuildResult(None, "leg_timestamp_skew", {"skew_seconds": skew})

        qff_close = qff.price if qff is not None and qff_is_fresh else None
        if qff_close is not None:
            self.last_qff_close = qff_close
        if self.last_qff_close is None:
            return MinuteBuildResult(None, "missing_qff_forward_fill")

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
            )
        )


class CcxtTickerMarketData:
    def __init__(self, exchange_id: str, timeout_ms: int = 30_000) -> None:
        import ccxt

        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class({"enableRateLimit": True, "timeout": timeout_ms})
        self.exchange.load_markets()
        self.exchange_id = exchange_id

    def fetch_quote(self, symbol: str) -> LiveQuote:
        ticker = self.exchange.fetch_ticker(symbol)
        price = parse_optional_float(ticker.get("last")) or parse_optional_float(
            ticker.get("close")
        )
        if price is None:
            raise RuntimeError(f"{self.exchange_id} ticker has no usable price: {ticker}")
        return LiveQuote(
            source=self.exchange_id,
            symbol=symbol,
            timestamp=parse_timestamp(ticker.get("timestamp")),
            price=price,
            bid=parse_optional_float(ticker.get("bid")),
            ask=parse_optional_float(ticker.get("ask")),
            raw=dict(ticker),
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


class FubonQffMarketData:
    def __init__(self, env_path: Path | None = None) -> None:
        self.env_path = env_path
        self.sdk = None
        self.intraday = None

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

    def close(self) -> None:
        if self.sdk is not None:
            self.sdk.logout()

    def _require_intraday(self) -> Any:
        if self.intraday is None:
            self.connect()
        return self.intraday

    def fetch_candidates(self, product: str) -> list[Any]:
        intraday = self._require_intraday()
        errors: list[str] = []
        for session in ("REGULAR", "AFTERHOURS"):
            try:
                result = intraday.tickers(
                    type="FUTURE", exchange="TAIFEX", session=session, product=product
                )
                data = result.get("data", result) if isinstance(result, dict) else result
                return list(data or [])
            except Exception as exc:
                errors.append(f"{session}: {exc}")
        raise RuntimeError(f"Fubon QFF ticker lookup failed: {errors}")

    def select_front_month_symbol(self, product: str) -> str:
        return select_qff_front_month(self.fetch_candidates(product), product=product).symbol

    def fetch_quote(self, symbol: str) -> LiveQuote:
        intraday = self._require_intraday()
        raw = intraday.quote(symbol=symbol)
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
        return LiveQuote(
            source="fubon_qff",
            symbol=symbol,
            timestamp=parse_timestamp(
                row_get(payload, "dateTime", "time", "timestamp", "lastUpdated")
            ),
            price=price,
            bid=first_float(payload, "bidPrice", "bestBidPrice", "bid"),
            ask=first_float(payload, "askPrice", "bestAskPrice", "ask"),
            raw=payload,
        )

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
