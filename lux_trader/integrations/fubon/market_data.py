from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from threading import Condition
from typing import Any

import pandas as pd

from ...core.calendar import in_night_session
from ...core.contracts import row_get, row_to_dict
from ...core.time import TAIPEI_TZ
from ...market_data.normalization import normalize_candle_rows
from ...market_data.parsing import (
    first_book_level,
    first_float,
    midpoint_or_single_side,
    parse_timestamp,
)
from ...market_data.session import select_ccf_front_month
from ...market_data.types import LiveQuote
from .auth import login_fubon_sdk

# Matches the live config's `ccf_book_stale_seconds` default. Measured
# 2026-08-15 over 22,579 book publishes across six sessions: p50 2.4s,
# p99 50.9s, max 478s. 600 clears that worst case while still catching a
# socket the peer closed inside ten minutes.
DEFAULT_BOOK_STALE_SECONDS = 600.0


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


class FubonCcfMarketData:
    def __init__(
        self,
        env_path: Path | None = None,
        *,
        book_wait_timeout_seconds: float = 5.0,
        book_stale_seconds: float = DEFAULT_BOOK_STALE_SECONDS,
    ) -> None:
        self.env_path = env_path
        self.sdk = None
        self.intraday = None
        self.websocket = None
        self.book_wait_timeout_seconds = book_wait_timeout_seconds
        # How long a cached book may go without a refresh before it stops
        # counting as a quote. A websocket the peer closed cleanly does not
        # always raise an error event, so without this the last book received
        # is served forever -- measured 2026-08-15, a CCF price frozen at
        # 120.75 for three hours while every poll recorded it as current.
        self.book_stale_seconds = float(book_stale_seconds)
        self._book_condition = Condition()
        self._latest_books: dict[str, LiveQuote] = {}
        # Arrival time, not the vendor's clock: this answers "is the stream
        # still delivering", which is the question a wedged socket breaks.
        self._book_received_at: dict[str, float] = {}
        self._book_subscription_ids: dict[str, str] = {}
        self._book_subscribed_symbols: set[str] = set()
        self._websocket_connected = False
        self._websocket_handlers_registered = False
        self.last_candidate_session_counts: dict[str, int] = {}
        self.last_candidate_session_summaries: dict[str, str] = {}
        self.last_cleanup_errors: list[str] = []

    def connect(self) -> None:
        from fubon_neo.sdk import FubonSDK, Mode

        sdk = FubonSDK()
        login_fubon_sdk(
            sdk,
            self.env_path,
            label="Fubon market data login",
            api_key_env="FUBON_MARKETDATA_API_KEY",
        )
        mode = getattr(Mode, "Normal", None)
        if mode is None:
            sdk.init_realtime()
        else:
            sdk.init_realtime(mode)
        self.sdk = sdk
        self.intraday = sdk.marketdata.rest_client.futopt.intraday
        self.websocket = sdk.marketdata.websocket_client.futopt

    def _record_cleanup_error(self, stage: str, exc: BaseException) -> None:
        """Keep a failed teardown visible.

        Both teardown steps used to swallow their exception silently. When a
        disconnect fails the old SDK's background threads outlive the object
        that owned them, and the only symptom is a socket nobody closes -- the
        2026-08-15 stall took hours to trace because nothing recorded this.
        Recording must never itself break a teardown, hence the cap and the
        absence of any raise.
        """
        entry = f"{stage}: {type(exc).__name__}: {exc}"
        self.last_cleanup_errors.append(entry)
        del self.last_cleanup_errors[:-8]

    def reconnect(self) -> None:
        # Drop the current (possibly token-expired) marketdata session and log in
        # again so the Fugle token is fresh. Called when entering a trading session
        # after an idle non-trading gap: an overnight token expires, and a bare
        # websocket restart would keep reusing the dead token (HTTP 401 Token expired).
        self.last_cleanup_errors = []
        self.teardown_books_session()
        old_sdk = self.sdk
        self.sdk = None
        self.intraday = None
        self.websocket = None
        self._websocket_handlers_registered = False
        if old_sdk is not None:
            try:
                old_sdk.logout()
            except Exception as exc:
                self._record_cleanup_error("sdk.logout", exc)
        self.connect()

    def close(self) -> None:
        if self.websocket is not None:
            try:
                self.websocket.disconnect()
            except Exception as exc:
                self._record_cleanup_error("websocket.disconnect", exc)
        if self.sdk is not None:
            self.sdk.logout()

    def teardown_books_session(self) -> None:
        with self._book_condition:
            self._latest_books.clear()
            self._book_received_at.clear()
            self._book_subscription_ids.clear()
            self._book_subscribed_symbols.clear()
            self._websocket_connected = False
            self._book_condition.notify_all()
        if self.websocket is not None:
            try:
                self.websocket.disconnect()
            except Exception as exc:
                self._record_cleanup_error("websocket.disconnect", exc)

    def restart_books_session(
        self,
        symbol: str,
        *,
        after_hours: bool | None = None,
    ) -> None:
        self.teardown_books_session()
        self.ensure_books_subscription(symbol, after_hours=after_hours)

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
            "Fubon CCF ticker lookup returned no candidates. "
            f"session_counts={counts}; session_summaries={summaries}; errors={errors}"
        )

    def select_front_month_symbol(self, product: str) -> str:
        return select_ccf_front_month(self.fetch_candidates(product), product=product).symbol

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
            self._book_received_at.pop(symbol, None)
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
            self._latest_books.clear()
            self._book_received_at.clear()
            self._book_subscription_ids.clear()
            self._book_subscribed_symbols.clear()
            self._websocket_connected = False
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
            self._book_received_at[quote.symbol] = time.monotonic()
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

    def _book_is_current(self, symbol: str) -> bool:
        """Whether the cached book for `symbol` is recent enough to serve.

        Caller must hold `_book_condition`.
        """
        if self.book_stale_seconds <= 0:
            return True
        received_at = self._book_received_at.get(symbol)
        if received_at is None:
            return False
        return (time.monotonic() - received_at) <= self.book_stale_seconds

    def _wait_for_book_quote(self, symbol: str) -> LiveQuote | None:
        deadline = time.monotonic() + self.book_wait_timeout_seconds
        with self._book_condition:
            quote = self._latest_books.get(symbol)
            # A book too old to trust is treated exactly as a missing one: wait
            # for the stream to deliver a fresh one, and fall through to the
            # REST path if it does not. Returning the stale copy instead is
            # what let a dead websocket look healthy indefinitely.
            while quote is None or not self._book_is_current(symbol):
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
            source="fubon_ccf",
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
        parts: list[pd.DataFrame] = []
        errors: dict[str, str] = {}
        for label, session in (("regular", None), ("afterhours", "afterhours")):
            params: dict[str, Any] = {"symbol": symbol, "timeframe": "1"}
            if session is not None:
                params["session"] = session
            try:
                raw = intraday.candles(**params)
                data = raw.get("data", raw) if isinstance(raw, dict) else raw
                frame = normalize_candle_rows(list(data or []), start, end)
                if not frame.empty:
                    parts.append(frame)
            except Exception as exc:
                errors[label] = str(exc)
        if not parts:
            if errors:
                raise RuntimeError(
                    "Fubon CCF candles returned no usable regular/after-hours "
                    f"data: {errors}"
                )
            return pd.DataFrame(columns=["timestamp", "close"])
        return (
            pd.concat(parts, ignore_index=True)
            .sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )



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
        source="fubon_ccf",
        symbol=symbol,
        timestamp=timestamp,
        # `price` here is the book midpoint, so its clock IS the book clock --
        # unlike IBKR, where price is a trade print and the two diverge. Saying
        # so explicitly rather than leaving it None matters: price_time() falls
        # back to `timestamp` either way, but the skew gate and every staleness
        # check read price_timestamp first, and a None there reads as "this
        # source has no price clock" rather than "they are the same clock".
        price_timestamp=timestamp,
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


