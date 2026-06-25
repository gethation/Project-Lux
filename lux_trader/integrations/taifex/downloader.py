from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zipfile import ZipFile

import pandas as pd

from ...core.time import TAIPEI_TZ, ensure_taipei
from ...market_data.session import qff_symbol_to_taifex_contract_month

TAIFEX_PREVIOUS_30_URL = (
    "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData"
)
TAIFEX_DAILY_CSV_LINK_PATTERN = re.compile(
    r"(?P<url>(?:https?://www\.taifex\.com\.tw)?"
    r"/file/taifex/Dailydownload/DailydownloadCSV/"
    r"Daily_(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2})\.zip)"
)


@dataclass(frozen=True)
class TaifexDownloadEntry:
    trading_date: date
    csv_url: str

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




def calendar_dates(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days
