"""Real USD/TWD from Twelve Data, for the CCF/UMC pair.

QFF/TSM prices its US leg in USDT and so uses BitoPro's USDT/TWD, which is
correct for that pair. CCF/UMC prices UMC in actual dollars, and BitoPro's rate
carries a 0.26% Taiwan-local crypto premium that no combination of crypto pairs
removes -- §8.2 of docs/MULTIPAIR_PLAN.md measures it at 77% of the spread's own
standard deviation. This pair needs a real FX quote.

Two measured facts shape this module (see scripts/probe_twelvedata.py):

* ``/quote`` returns a date-only ``datetime`` for forex, so anything derived from
  it about freshness is fiction. Everything here reads ``time_series`` instead.
* The free tier allows 8 credits per minute and 800 per day, while the live loop
  polls once per second. Callers must wrap this in a CachedQuoteProvider; this
  class deliberately does not cache, so the cache policy stays visible in one
  place rather than hidden behind a provider.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import requests

from ...core.time import TAIPEI_TZ, ensure_taipei
from ...market_data.types import LiveQuote


BASE_URL = "https://api.twelvedata.com"
API_KEY_ENV = "TWELVEDATA_API_KEY"
SOURCE = "twelvedata"

# Twelve Data caps a single time_series response; larger ranges must be chunked.
MAX_OUTPUTSIZE = 5000


class TwelveDataError(RuntimeError):
    """Any refusal from the API, including a payload-level error object."""


class TwelveDataRateLimited(TwelveDataError):
    """Credit limit hit. Distinct so callers can back off instead of failing."""


def _require_api_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    value = os.getenv(API_KEY_ENV, "").strip()
    if not value:
        raise TwelveDataError(f"Missing required environment variable: {API_KEY_ENV}")
    return value


def _parse_bar_open(text: str) -> datetime:
    """Twelve Data timestamps are naive and in the timezone that was requested."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=TAIPEI_TZ)
        except ValueError:
            continue
    raise TwelveDataError(f"Unparsable timestamp from Twelve Data: {text!r}")


class TwelveDataMarketData:
    """Quote and 1m history for a forex pair such as ``USD/TWD``."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 15.0,
        session: Any | None = None,
    ) -> None:
        self.api_key = _require_api_key(api_key)
        self.timeout_seconds = float(timeout_seconds)
        self.session = session or requests.Session()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params["apikey"] = self.api_key
        try:
            response = self.session.get(
                f"{BASE_URL}/{path}",
                params=params,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise TwelveDataError(f"Twelve Data {path} request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TwelveDataError(
                f"Twelve Data {path} returned non-JSON "
                f"(HTTP {response.status_code}): {response.text[:200]}"
            ) from exc

        # Errors arrive as a 200 with an error object, so status code alone is not
        # enough to tell success from failure.
        if isinstance(payload, dict) and payload.get("status") == "error":
            code = payload.get("code")
            message = payload.get("message", "")
            if code == 429:
                raise TwelveDataRateLimited(f"Twelve Data credit limit hit: {message}")
            raise TwelveDataError(f"Twelve Data {path} error {code}: {message}")
        if not isinstance(payload, dict):
            raise TwelveDataError(f"Twelve Data {path} returned {type(payload).__name__}")
        return payload

    def fetch_quote(self, symbol: str) -> LiveQuote:
        """Newest complete 1m bar, stamped with the moment it became valid.

        The timestamp is the bar's *close*, not its label. Twelve Data labels a
        bar by its opening minute, so a bar labelled 06:41 describes 06:41:00 to
        06:42:00 and its close is the earliest instant the value is fully known.
        Stamping the label instead would understate freshness by a whole minute
        against a staleness gate that compares to a minute's close time.
        """
        payload = self._get(
            "time_series",
            symbol=symbol,
            interval="1min",
            outputsize=1,
            timezone="Asia/Taipei",
        )
        values = payload.get("values") or []
        if not values:
            raise TwelveDataError(f"Twelve Data returned no bars for {symbol!r}")
        row = values[0]
        close = row.get("close")
        if close is None:
            raise TwelveDataError(f"Twelve Data bar for {symbol!r} has no close: {row}")
        bar_open = _parse_bar_open(str(row["datetime"]))
        bar_close = bar_open + timedelta(minutes=1)
        fetched_at = datetime.now(TAIPEI_TZ)
        return LiveQuote(
            source=SOURCE,
            symbol=symbol,
            timestamp=bar_close,
            price=float(close),
            raw={
                "bar_open": bar_open.isoformat(),
                "bar_close": bar_close.isoformat(),
                "fetched_at": fetched_at.isoformat(),
                "source_lag_seconds": (fetched_at - bar_close).total_seconds(),
                "interval": "1min",
                "row": row,
            },
        )

    def fetch_ohlcv_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """1m closes between ``start`` and ``end``, both inclusive, Taipei time.

        Walks backwards in chunks because the API returns newest-first and caps
        each response, and forex has no bars over the weekend, so a fixed bar
        count cannot be mapped onto a wall-clock range.
        """
        start_tpe = ensure_taipei(start)
        end_tpe = ensure_taipei(end)
        if start_tpe > end_tpe:
            raise ValueError("start must not be after end")

        collected: list[dict[str, Any]] = []
        cursor = end_tpe
        seen: set[str] = set()
        while cursor >= start_tpe:
            payload = self._get(
                "time_series",
                symbol=symbol,
                interval="1min",
                outputsize=MAX_OUTPUTSIZE,
                timezone="Asia/Taipei",
                start_date=start_tpe.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=cursor.strftime("%Y-%m-%d %H:%M:%S"),
            )
            values = payload.get("values") or []
            if not values:
                break
            fresh = [row for row in values if row["datetime"] not in seen]
            if not fresh:
                break
            for row in fresh:
                seen.add(row["datetime"])
                collected.append(row)
            oldest = _parse_bar_open(str(fresh[-1]["datetime"]))
            if oldest <= start_tpe or len(values) < MAX_OUTPUTSIZE:
                break
            cursor = oldest - timedelta(minutes=1)

        if not collected:
            return pd.DataFrame(columns=["timestamp", "close"])

        frame = pd.DataFrame(
            {
                "timestamp": [
                    _parse_bar_open(str(row["datetime"])) for row in collected
                ],
                "close": [float(row["close"]) for row in collected],
            }
        )
        frame = frame.drop_duplicates("timestamp").sort_values("timestamp")
        mask = (frame["timestamp"] >= start_tpe) & (frame["timestamp"] <= end_tpe)
        return frame.loc[mask].reset_index(drop=True)

    def api_usage(self) -> dict[str, Any]:
        """Remaining free-tier credits, for the doctor command and for tests."""
        return self._get("api_usage")
