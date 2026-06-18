from __future__ import annotations

import io
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import pytest

from lux_trader.config import AppConfig, LiveMarketDataConfig, SafetyConfig
from lux_trader.live_market_data import (
    CcxtTickerMarketData,
    LiveMinuteBarBuilder,
    LiveQuote,
    LiveQuoteSet,
    FubonQffMarketData,
    TaifexQffTradeDownloader,
    WarmupBuilder,
    build_qff_warmup_source_report,
    parse_fubon_books_quote,
    parse_timestamp,
    parse_taifex_download_entries,
    qff_symbol_to_taifex_contract_month,
    select_qff_front_month,
)
from lux_trader.live_runner import (
    LivePaperRunner,
    QffContractResolution,
    QffWarmupCheckRunner,
    WarmupRunner,
    build_live_decision_snapshot,
    cancel_entry_pending_for_contract_switch,
    mark_pending_contract_switch_if_needed,
    should_force_exit_for_contract_policy,
    should_switch_contract_before_processing,
)
from lux_trader.models import Direction, IndicatorSnapshot, StrategyState
from lux_trader.strategy import StrategyRuntimeState
from lux_trader.store import SQLiteStore
from lux_trader.terminal_ui import LiveTerminalReporter
from lux_trader.tradable_spread import TradableSpreadSnapshot

from conftest import make_app_config


class FakeQffProvider:
    def __init__(self, rows: pd.DataFrame | None = None, quotes: list[LiveQuote] | None = None) -> None:
        self.rows = rows if rows is not None else pd.DataFrame(columns=["timestamp", "close"])
        self.quotes = list(quotes or [])
        self.select_calls = 0
        self.fetch_1m_calls: list[tuple[str, datetime, datetime]] = []

    def select_front_month_symbol(self, product: str) -> str:
        self.select_calls += 1
        return "QFF202607"

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.fetch_1m_calls.append((symbol, start, end))
        return self.rows.copy()

    def fetch_quote(self, symbol: str) -> LiveQuote:
        if not self.quotes:
            raise RuntimeError("No fake QFF quotes left")
        return self.quotes.pop(0)


class FakeOhlcvProvider:
    def __init__(self, rows: pd.DataFrame, quotes: list[LiveQuote] | None = None) -> None:
        self.rows = rows
        self.quotes = list(quotes or [])
        self.fetch_ohlcv_calls: list[tuple[str, datetime, datetime]] = []

    def fetch_ohlcv_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.fetch_ohlcv_calls.append((symbol, start, end))
        return self.rows.copy()

    def fetch_quote(self, symbol: str) -> LiveQuote:
        if not self.quotes:
            raise RuntimeError("No fake quotes left")
        return self.quotes.pop(0)


class FakeFubonIntraday:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.sessions: list[str] = []
        self.quote_calls: list[dict[str, object]] = []

    def tickers(self, *, type: str, exchange: str, session: str, product: str) -> object:
        self.sessions.append(session)
        response = self.responses[session]
        if isinstance(response, Exception):
            raise response
        return response

    def quote(self, **kwargs: object) -> object:
        self.quote_calls.append(kwargs)
        return self.responses.get(
            "quote",
            {
                "data": {
                    "symbol": kwargs.get("symbol"),
                    "closePrice": 2410.0,
                    "lastTrade": {"bid": 2409.0, "ask": 2411.0},
                    "lastUpdated": "2026-06-18T08:45:01+08:00",
                }
            },
        )


class FakeCcxtExchange:
    def __init__(
        self,
        *,
        order_book: dict[str, object],
        ticker: dict[str, object] | None = None,
        fail_first_order_book: Exception | None = None,
    ) -> None:
        self.order_book = order_book
        self.ticker = ticker or {"last": 20.0, "timestamp": 1781743501000}
        self.fail_first_order_book = fail_first_order_book
        self.fetch_order_book_calls: list[tuple[str, int | None]] = []
        self.fetch_ticker_calls: list[str] = []

    def fetch_order_book(self, symbol: str, limit: int | None = None) -> dict[str, object]:
        self.fetch_order_book_calls.append((symbol, limit))
        if self.fail_first_order_book is not None:
            exc = self.fail_first_order_book
            self.fail_first_order_book = None
            raise exc
        return self.order_book

    def fetch_ticker(self, symbol: str) -> dict[str, object]:
        self.fetch_ticker_calls.append(symbol)
        return self.ticker


class FakeFubonWebSocket:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}
        self.connected = False
        self.subscriptions: list[dict[str, object]] = []
        self.unsubscriptions: list[dict[str, object]] = []
        self.disconnected = False

    def on(self, event: str, listener: object) -> None:
        self.listeners[event] = listener

    def connect(self) -> None:
        self.connected = True

    def subscribe(self, params: dict[str, object]) -> None:
        self.subscriptions.append(params)

    def unsubscribe(self, params: dict[str, object]) -> None:
        self.unsubscriptions.append(params)

    def disconnect(self) -> None:
        self.disconnected = True

    def emit(self, message: dict[str, object]) -> None:
        listener = self.listeners["message"]
        listener(message)  # type: ignore[misc]


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def rows(values: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"timestamp": [ts(timestamp) for timestamp, _ in values], "close": [value for _, value in values]}
    )


def quote(
    source: str,
    timestamp: str,
    price: float,
    *,
    bid: float | None = None,
    ask: float | None = None,
) -> LiveQuote:
    return LiveQuote(
        source=source,
        symbol=source,
        timestamp=ts(timestamp),
        price=price,
        bid=bid,
        ask=ask,
    )


def make_taifex_zip(csv_text: str) -> bytes:
    payload = io.BytesIO()
    with ZipFile(payload, "w") as zip_file:
        zip_file.writestr("Daily_2026_06_18.csv", csv_text.encode("cp950"))
    return payload.getvalue()


def small_live_config(tmp_path: Path) -> AppConfig:
    base = make_app_config(tmp_path, validate_expected_zscore=False)
    return replace(
        base,
        strategy=replace(base.strategy, zscore_window=3),
        live=LiveMarketDataConfig(
            polling_seconds=0.0,
            minute_finalize_delay_seconds=1.0,
            stale_seconds=10.0,
            max_leg_timestamp_skew_seconds=10.0,
            warmup_minutes=3,
            qff_product="QFF",
            qff_symbol="auto",
            binance_symbol="TSM/USDT:USDT",
            bitopro_symbol="USDT/TWD",
            fubon_env_path=None,
            taifex_qff_1m_csv=None,
            taifex_use_network=False,
            taifex_cache_dir=tmp_path / "taifex_cache",
        ),
    )


def count_table(store: SQLiteStore, table: str) -> int:
    row = store.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def indicator_snapshot(zscore: float = 2.1) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        timestamp=ts("2026-06-18T08:45:00+08:00"),
        spread=2.1,
        mean=0.0,
        std=1.0,
        zscore=zscore,
        zscore_valid=True,
        entry_allowed=True,
        close_allowed=True,
        friday_night_close_only=False,
    )


def test_fubon_fetch_candidates_checks_afterhours_when_regular_is_empty() -> None:
    provider = FubonQffMarketData(None)
    intraday = FakeFubonIntraday(
        {
            "REGULAR": {"data": []},
            "AFTERHOURS": {
                "data": [
                    {
                        "symbol": "QFFH6",
                        "product": "QFF",
                        "endDate": "2026-08-19",
                    }
                ]
            },
        }
    )
    provider.intraday = intraday

    candidates = provider.fetch_candidates("QFF")

    assert intraday.sessions == ["REGULAR", "AFTERHOURS"]
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "QFFH6"
    assert provider.last_candidate_session_counts == {
        "REGULAR": 0,
        "AFTERHOURS": 1,
    }
    assert "sample=" in provider.last_candidate_session_summaries["AFTERHOURS"]


def test_fubon_fetch_candidates_empty_sessions_error_includes_diagnostics() -> None:
    provider = FubonQffMarketData(None)
    intraday = FakeFubonIntraday(
        {
            "REGULAR": {"data": []},
            "AFTERHOURS": {"data": []},
        }
    )
    provider.intraday = intraday

    with pytest.raises(RuntimeError) as error:
        provider.fetch_candidates("QFF")

    message = str(error.value)
    assert intraday.sessions == ["REGULAR", "AFTERHOURS"]
    assert "session_counts={'REGULAR': 0, 'AFTERHOURS': 0}" in message
    assert "session_summaries=" in message
    assert provider.last_candidate_session_counts == {
        "REGULAR": 0,
        "AFTERHOURS": 0,
    }


def test_select_qff_front_month_skips_expired_contracts() -> None:
    selected = select_qff_front_month(
        [
            {"symbol": "QFF202606", "contractMonth": "202606"},
            {"symbol": "QFF202608", "contractMonth": "202608"},
            {"symbol": "QFF202607", "contractMonth": "202607"},
        ],
        product="QFF",
        today=datetime.fromisoformat("2026-06-18T00:00:00+08:00").date(),
    )

    assert selected.symbol == "QFF202607"


def test_select_qff_front_month_accepts_fubon_end_date_fields() -> None:
    selected = select_qff_front_month(
        [
            {"symbol": "QFFG6", "endDate": "2026-07-15"},
            {"symbol": "QFFH6", "settlementDate": "2026-08-19"},
            {"symbol": "QFFC7", "endDate": "2027-03-17"},
        ],
        product="QFF",
        today=datetime.fromisoformat("2026-06-18T00:00:00+08:00").date(),
    )

    assert selected.symbol == "QFFG6"


def test_select_qff_front_month_fails_when_expiry_is_unparseable() -> None:
    with pytest.raises(RuntimeError, match="Unable to select"):
        select_qff_front_month([{"symbol": "QFFUNKNOWN"}], product="QFF")


def test_qff_symbol_to_taifex_contract_month_accepts_fubon_code() -> None:
    assert (
        qff_symbol_to_taifex_contract_month(
            "QFFG6",
            reference_date=datetime.fromisoformat("2026-06-18T00:00:00+08:00").date(),
        )
        == "202607"
    )


def test_parse_timestamp_accepts_fubon_microsecond_epoch() -> None:
    parsed = parse_timestamp(1781760623530000)

    assert parsed == ts("2026-06-18T13:30:23.530000+08:00")


def test_ccxt_quote_uses_top_of_book_for_bid_ask() -> None:
    provider = object.__new__(CcxtTickerMarketData)
    provider.exchange_id = "fake"
    provider.exchange = FakeCcxtExchange(
        order_book={
            "timestamp": 1781743501000,
            "bids": [[20.12, 30.0]],
            "asks": [[20.14, 25.0]],
        }
    )

    fetched = provider.fetch_quote("TSM/USDT:USDT")

    assert fetched.price == pytest.approx(20.13)
    assert fetched.bid == 20.12
    assert fetched.ask == 20.14
    assert fetched.bid_size == 30.0
    assert fetched.ask_size == 25.0
    assert fetched.raw is not None
    assert fetched.raw["book_missing"] is False
    assert provider.exchange.fetch_order_book_calls == [("TSM/USDT:USDT", 1)]
    assert provider.exchange.fetch_ticker_calls == []


def test_ccxt_quote_retries_binance_invalid_depth_with_supported_limit() -> None:
    provider = object.__new__(CcxtTickerMarketData)
    provider.exchange_id = "binanceusdm"
    provider.exchange = FakeCcxtExchange(
        order_book={
            "timestamp": 1781743501000,
            "bids": [[459.6, 0.75]],
            "asks": [[459.7, 1.15]],
        },
        fail_first_order_book=RuntimeError(
            'binanceusdm {"code":-4021,"msg":"1 is not valid depth limit"}'
        ),
    )

    fetched = provider.fetch_quote("TSM/USDT:USDT")

    assert fetched.bid == 459.6
    assert fetched.ask == 459.7
    assert fetched.raw is not None
    assert fetched.raw["book_limit_used"] == 5
    assert provider.exchange.fetch_order_book_calls == [
        ("TSM/USDT:USDT", 1),
        ("TSM/USDT:USDT", 5),
    ]


def test_ccxt_quote_empty_book_does_not_fallback_to_ticker_bid_ask() -> None:
    provider = object.__new__(CcxtTickerMarketData)
    provider.exchange_id = "fake"
    provider.exchange = FakeCcxtExchange(
        order_book={"bids": [], "asks": []},
        ticker={"last": 20.5, "bid": 20.4, "ask": 20.6, "timestamp": 1781743501000},
    )

    fetched = provider.fetch_quote("TSM/USDT:USDT")

    assert fetched.price == 20.5
    assert fetched.bid is None
    assert fetched.ask is None
    assert fetched.raw is not None
    assert fetched.raw["book_missing"] is True
    assert provider.exchange.fetch_ticker_calls == ["TSM/USDT:USDT"]


def test_parse_fubon_books_quote_reads_top_level_bid_ask_and_sizes() -> None:
    fetched = parse_fubon_books_quote(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "QFFG6",
                "time": 1781743501000000,
                "bids": [{"price": 2409.0, "size": 12}],
                "asks": [{"price": 2411.0, "size": 8}],
            },
        }
    )

    assert fetched is not None
    assert fetched.symbol == "QFFG6"
    assert fetched.price == 2410.0
    assert fetched.bid == 2409.0
    assert fetched.ask == 2411.0
    assert fetched.bid_size == 12.0
    assert fetched.ask_size == 8.0


def test_fubon_books_cache_fetch_quote_returns_latest_book() -> None:
    provider = FubonQffMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    websocket = FakeFubonWebSocket()
    provider.websocket = websocket

    provider.ensure_books_subscription("QFFG6", after_hours=True)
    websocket.emit(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "QFFG6",
                "time": "2026-06-18T08:45:01+08:00",
                "bids": [{"price": 2409.0, "size": 3}],
                "asks": [{"price": 2411.0, "size": 4}],
            },
        }
    )

    fetched = provider.fetch_quote("QFFG6")

    assert websocket.connected
    assert websocket.subscriptions == [
        {"channel": "books", "symbol": "QFFG6", "afterHours": True}
    ]
    assert fetched.bid == 2409.0
    assert fetched.ask == 2411.0
    assert fetched.bid_size == 3.0
    assert fetched.ask_size == 4.0


def test_fubon_books_subscription_can_unsubscribe_old_symbol() -> None:
    provider = FubonQffMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    websocket = FakeFubonWebSocket()
    provider.websocket = websocket

    provider.ensure_books_subscription("QFFG6", after_hours=False)
    websocket.emit(
        {
            "event": "subscribed",
            "channel": "books",
            "data": {"symbol": "QFFG6", "channel": "books", "id": "sub-1"},
        }
    )
    provider.unsubscribe_books("QFFG6")

    assert websocket.unsubscriptions == [{"id": "sub-1"}]


def test_fubon_quote_rest_diagnostics_does_not_fill_book_fields() -> None:
    provider = FubonQffMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    provider.websocket = FakeFubonWebSocket()

    fetched = provider.fetch_quote("QFFG6")

    assert fetched.price == 2410.0
    assert fetched.bid is None
    assert fetched.ask is None
    assert fetched.raw is not None
    assert fetched.raw["rest_last_trade_bid"] == 2409.0
    assert fetched.raw["rest_last_trade_ask"] == 2411.0
    assert fetched.raw["book_missing"] is True


def test_parse_taifex_download_entries_extracts_csv_links() -> None:
    entries = parse_taifex_download_entries(
        """
        <input onClick="javascript:window.open(
        'https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_2026_06_18.zip')">
        <input onClick="javascript:window.open(
        '/file/taifex/Dailydownload/DailydownloadCSV/Daily_2026_06_17.zip')">
        """
    )

    assert [entry.trading_date.isoformat() for entry in entries] == [
        "2026-06-17",
        "2026-06-18",
    ]
    assert entries[-1].csv_url.endswith("Daily_2026_06_18.zip")


def test_taifex_qff_trade_downloader_aggregates_tick_csv_to_1m(tmp_path) -> None:
    zip_payload = make_taifex_zip(
        "\n".join(
            [
                "成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),近月價格,遠月價格,開盤集合競價 ",
                "20260617,QFF    ,202607     ,172500,2415,150,-,-,*",
                "20260617,QFF    ,202607     ,172513,2420,2,-,-, ",
                "20260617,QFF    ,202608     ,172530,2500,2,-,-, ",
                "20260617,TX     ,202607     ,172545,23000,2,-,-, ",
            ]
        )
    )

    def http_get(url: str) -> bytes:
        if url.endswith(".zip"):
            return zip_payload
        return (
            "https://www.taifex.com.tw/file/taifex/Dailydownload/"
            "DailydownloadCSV/Daily_2026_06_18.zip"
        ).encode("utf-8")

    frame = TaifexQffTradeDownloader(
        tmp_path / "cache",
        http_get=http_get,
    ).fetch_1m(
        "QFFG6",
        ts("2026-06-17T17:25:00+08:00"),
        ts("2026-06-17T17:26:00+08:00"),
    )

    assert len(frame) == 1
    assert frame.iloc[0]["timestamp"] == pd.Timestamp("2026-06-17T17:25:00+08:00")
    assert frame.iloc[0]["close"] == 2420.0


def test_warmup_builder_combines_qff_sources_and_forward_fills(tmp_path) -> None:
    config = small_live_config(tmp_path)
    fallback = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 100.0),
                ("2026-06-18T08:47:00+08:00", 102.0),
            ]
        )
    )
    intraday = FakeQffProvider(rows([("2026-06-18T08:47:00+08:00", 103.0)]))
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 20.0),
                ("2026-06-18T08:46:00+08:00", 20.5),
                ("2026-06-18T08:47:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 30.0),
                ("2026-06-18T08:46:00+08:00", 30.0),
                ("2026-06-18T08:47:00+08:00", 30.0),
            ]
        )
    )

    bars = WarmupBuilder(
        live_config=config.live,
        qff_intraday_provider=intraday,
        qff_fallback_provider=fallback,
        tsm_provider=tsm,
        usdttwd_provider=usd,
    ).build(qff_symbol="QFF202607", end=ts("2026-06-18T08:48:42+08:00"))

    assert len(bars) == 3
    assert [bar.timestamp for bar in bars] == [
        ts("2026-06-18T08:45:00+08:00"),
        ts("2026-06-18T08:46:00+08:00"),
        ts("2026-06-18T08:47:00+08:00"),
    ]
    assert bars[1].qff_close is None
    assert bars[1].qff_close_filled == 100.0
    assert bars[2].qff_close_filled == 103.0
    assert bars[2].tsm_twd_fair == 21.0 * 30.0 / 5.0
    assert bars[2].spread == pytest.approx((bars[2].tsm_twd_fair - 103.0) / (bars[2].tsm_twd_fair + 103.0) * 200.0)


def test_qff_warmup_source_report_tracks_precedence_and_quality() -> None:
    report = build_qff_warmup_source_report(
        [
            (
                "taifex",
                rows(
                    [
                        ("2026-06-18T08:44:00+08:00", 99.0),
                        ("2026-06-18T08:45:00+08:00", 100.0),
                        ("2026-06-18T08:47:00+08:00", 102.0),
                    ]
                ),
            ),
            ("fubon", rows([("2026-06-18T08:47:00+08:00", 103.0)])),
        ],
        start_minute=ts("2026-06-18T08:45:00+08:00"),
        end_minute=ts("2026-06-18T08:47:00+08:00"),
        qff_fetch_start=ts("2026-06-18T08:44:00+08:00"),
    )

    assert report.null_count == 0
    assert report.mismatch_count == 1
    assert report.max_abs_diff == 1.0
    assert report.frame.loc[0, "qff_close_filled"] == 100.0
    assert report.frame.loc[1, "qff_close_filled"] == 100.0
    assert report.frame.loc[1, "source_used"] == "forward_fill"
    assert report.frame.loc[2, "merged_qff_close"] == 103.0
    assert report.frame.loc[2, "source_used"] == "fubon"


def test_warmup_builder_uses_prior_qff_close_to_seed_forward_fill(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:44:00+08:00", 99.0),
                ("2026-06-18T08:46:00+08:00", 101.0),
            ]
        )
    )
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 20.0),
                ("2026-06-18T08:46:00+08:00", 20.0),
                ("2026-06-18T08:47:00+08:00", 20.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 30.0),
                ("2026-06-18T08:46:00+08:00", 30.0),
                ("2026-06-18T08:47:00+08:00", 30.0),
            ]
        )
    )

    bars = WarmupBuilder(
        live_config=config.live,
        qff_intraday_provider=qff,
        qff_fallback_provider=None,
        tsm_provider=tsm,
        usdttwd_provider=usd,
    ).build(qff_symbol="QFF202607", end=ts("2026-06-18T08:48:00+08:00"))

    assert bars[0].qff_close is None
    assert bars[0].qff_close_filled == 99.0
    assert bars[1].qff_close_filled == 101.0


def test_warmup_builder_fails_when_initial_qff_cannot_be_filled(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(rows([("2026-06-18T08:46:00+08:00", 101.0)]))
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 20.0),
                ("2026-06-18T08:46:00+08:00", 20.0),
                ("2026-06-18T08:47:00+08:00", 20.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 30.0),
                ("2026-06-18T08:46:00+08:00", 30.0),
                ("2026-06-18T08:47:00+08:00", 30.0),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="QFF warmup cannot forward-fill"):
        WarmupBuilder(
            live_config=config.live,
            qff_intraday_provider=qff,
            qff_fallback_provider=None,
            tsm_provider=tsm,
            usdttwd_provider=usd,
        ).build(qff_symbol="QFF202607", end=ts("2026-06-18T08:48:00+08:00"))


def test_warmup_builder_fails_when_tsm_or_usd_is_missing(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(rows([("2026-06-18T08:45:00+08:00", 100.0)]))
    tsm = FakeOhlcvProvider(rows([("2026-06-18T08:45:00+08:00", 20.0)]))
    usd = FakeOhlcvProvider(rows([("2026-06-18T08:45:00+08:00", 30.0)]))

    with pytest.raises(RuntimeError, match="missing minutes"):
        WarmupBuilder(
            live_config=config.live,
            qff_intraday_provider=qff,
            qff_fallback_provider=None,
            tsm_provider=tsm,
            usdttwd_provider=usd,
        ).build(qff_symbol="QFF202607", end=ts("2026-06-18T08:48:00+08:00"))


def test_live_minute_bar_builder_finalizes_on_minute_crossing() -> None:
    builder = LiveMinuteBarBuilder(stale_seconds=10.0, max_leg_timestamp_skew_seconds=10.0)
    first = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:45:55+08:00", 100.0),
        tsm=quote("tsm", "2026-06-18T08:45:55+08:00", 20.0),
        usdttwd=quote("usd", "2026-06-18T08:45:55+08:00", 30.0),
    )
    second = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:46:01+08:00", 101.0),
        tsm=quote("tsm", "2026-06-18T08:46:01+08:00", 21.0),
        usdttwd=quote("usd", "2026-06-18T08:46:01+08:00", 30.0),
    )

    assert builder.update(first, ts("2026-06-18T08:45:55+08:00")) is None
    result = builder.update(second, ts("2026-06-18T08:46:01+08:00"))

    assert result is not None
    assert result.bar is not None
    assert result.bar.timestamp == ts("2026-06-18T08:45:00+08:00")
    assert result.bar.qff_close_filled == 100.0


def test_live_minute_bar_builder_allows_qff_forward_fill_but_skips_stale_tsm() -> None:
    builder = LiveMinuteBarBuilder(stale_seconds=10.0, max_leg_timestamp_skew_seconds=10.0)
    first = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:45:55+08:00", 100.0),
        tsm=quote("tsm", "2026-06-18T08:45:00+08:00", 20.0),
        usdttwd=quote("usd", "2026-06-18T08:45:55+08:00", 30.0),
    )
    stale = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:46:01+08:00", 101.0),
        tsm=quote("tsm", "2026-06-18T08:46:01+08:00", 21.0),
        usdttwd=quote("usd", "2026-06-18T08:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T08:45:55+08:00"))
    result = builder.update(stale, ts("2026-06-18T08:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason == "market_data_stale"


def test_live_minute_bar_builder_forward_fills_stale_qff_quote() -> None:
    builder = LiveMinuteBarBuilder(stale_seconds=10.0, max_leg_timestamp_skew_seconds=10.0)
    builder.last_qff_close = 99.0
    first = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:44:00+08:00", 100.0),
        tsm=quote("tsm", "2026-06-18T08:45:59+08:00", 20.0),
        usdttwd=quote("usd", "2026-06-18T08:45:59+08:00", 30.0),
    )
    second = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:46:01+08:00", 101.0),
        tsm=quote("tsm", "2026-06-18T08:46:01+08:00", 21.0),
        usdttwd=quote("usd", "2026-06-18T08:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T08:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T08:46:01+08:00"))

    assert result is not None
    assert result.bar is not None
    assert result.bar.qff_close is None
    assert result.bar.qff_close_filled == 99.0


def test_live_paper_runner_uses_paper_broker_and_minute_boundaries(tmp_path) -> None:
    config = small_live_config(tmp_path)
    warmup_qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 100.0),
                ("2026-06-18T08:43:00+08:00", 100.0),
                ("2026-06-18T08:44:00+08:00", 100.0),
            ]
        )
    )
    warmup_tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 20.0),
                ("2026-06-18T08:43:00+08:00", 20.0),
                ("2026-06-18T08:44:00+08:00", 20.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 30.0),
                ("2026-06-18T08:43:00+08:00", 30.0),
                ("2026-06-18T08:44:00+08:00", 30.0),
            ]
        )
    )
    WarmupRunner(
        config,
        qff_provider=warmup_qff,
        qff_fallback_provider=None,
        tsm_provider=warmup_tsm,
        usdttwd_provider=warmup_usd,
    ).run(reset_store=True, end=ts("2026-06-18T08:45:00+08:00"))

    clocks = iter(
        [
            ts("2026-06-18T08:45:30+08:00"),
            ts("2026-06-18T08:45:59+08:00"),
            ts("2026-06-18T08:46:00+08:00"),
            ts("2026-06-18T08:46:01+08:00"),
            ts("2026-06-18T08:46:01+08:00"),
        ]
    )
    qff = FakeQffProvider(
        quotes=[
            quote("qff", "2026-06-18T08:45:59+08:00", 100.0, bid=99.9, ask=100.1),
            quote("qff", "2026-06-18T08:46:00+08:00", 100.0, bid=99.9, ask=100.1),
            quote("qff", "2026-06-18T08:46:01+08:00", 100.0, bid=99.9, ask=100.1),
        ]
    )
    tsm = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("tsm", "2026-06-18T08:45:59+08:00", 20.0, bid=19.99, ask=20.01),
            quote("tsm", "2026-06-18T08:46:00+08:00", 20.0, bid=19.99, ask=20.01),
            quote("tsm", "2026-06-18T08:46:01+08:00", 20.0, bid=19.99, ask=20.01),
        ],
    )
    usd = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("usd", "2026-06-18T08:45:59+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-18T08:46:00+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-18T08:46:01+08:00", 30.0, bid=29.99, ask=30.01),
        ],
    )
    terminal_output = io.StringIO()
    reporter = LiveTerminalReporter(terminal_output, color=False)

    result = LivePaperRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=lambda: next(clocks),
        sleeper=lambda _: None,
        reporter=reporter,
    ).run(resume=True, max_iterations=3)

    assert result.bars_processed == 1
    output = terminal_output.getvalue()
    assert output.count("LIVE") == 3
    assert "08:45 BAR  " in output
    assert "shortSpread(spread=" in output
    assert "longSpread(spread=" in output
    assert "none" in output
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        summary = store.build_summary(config.strategy, config.fees)
        assert summary["rows"] == 1
        assert summary["trade_count"] == 0
        row = store.connection.execute(
            """
            SELECT short_spread, short_zscore, long_spread, long_zscore,
                   decision_spread_type, decision_zscore
            FROM bars
            """
        ).fetchone()
        assert row["short_spread"] is not None
        assert row["long_spread"] is not None
        assert row["decision_spread_type"] is None
        assert row["decision_zscore"] is None
    finally:
        store.close()


def test_live_paper_terminal_reporter_warns_on_stale_minute(tmp_path) -> None:
    config = small_live_config(tmp_path)
    warmup_qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 100.0),
                ("2026-06-18T08:43:00+08:00", 100.0),
                ("2026-06-18T08:44:00+08:00", 100.0),
            ]
        )
    )
    warmup_tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 20.0),
                ("2026-06-18T08:43:00+08:00", 20.0),
                ("2026-06-18T08:44:00+08:00", 20.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 30.0),
                ("2026-06-18T08:43:00+08:00", 30.0),
                ("2026-06-18T08:44:00+08:00", 30.0),
            ]
        )
    )
    WarmupRunner(
        config,
        qff_provider=warmup_qff,
        qff_fallback_provider=None,
        tsm_provider=warmup_tsm,
        usdttwd_provider=warmup_usd,
    ).run(reset_store=True, end=ts("2026-06-18T08:45:00+08:00"))

    clocks = iter(
        [
            ts("2026-06-18T08:45:30+08:00"),
            ts("2026-06-18T08:45:59+08:00"),
            ts("2026-06-18T08:46:01+08:00"),
            ts("2026-06-18T08:46:01+08:00"),
        ]
    )
    qff = FakeQffProvider(
        quotes=[
            quote("qff", "2026-06-18T08:45:59+08:00", 100.0),
            quote("qff", "2026-06-18T08:46:01+08:00", 100.0),
        ]
    )
    tsm = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("tsm", "2026-06-18T08:45:40+08:00", 20.0),
            quote("tsm", "2026-06-18T08:46:01+08:00", 20.0),
        ],
    )
    usd = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("usd", "2026-06-18T08:45:59+08:00", 30.0),
            quote("usd", "2026-06-18T08:46:01+08:00", 30.0),
        ],
    )
    terminal_output = io.StringIO()

    result = LivePaperRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=lambda: next(clocks),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(resume=True, max_iterations=2)

    assert result.bars_processed == 0
    assert result.skipped_minutes == 1
    assert "WARN stale_tsm skipped_minute" in terminal_output.getvalue()


def test_live_paper_auto_warmup_builds_seed_on_empty_store(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 100.0),
                ("2026-06-18T08:43:00+08:00", 101.0),
                ("2026-06-18T08:44:00+08:00", 102.0),
            ]
        )
    )
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 20.0),
                ("2026-06-18T08:43:00+08:00", 20.5),
                ("2026-06-18T08:44:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 30.0),
                ("2026-06-18T08:43:00+08:00", 30.0),
                ("2026-06-18T08:44:00+08:00", 30.0),
            ]
        )
    )
    terminal_output = io.StringIO()

    result = LivePaperRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=lambda: ts("2026-06-18T08:45:00+08:00"),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(reset_store=True, max_iterations=0)

    assert result.iterations == 0
    assert result.qff_symbol == "QFF202607"
    assert qff.fetch_1m_calls
    output = terminal_output.getvalue()
    assert "EVENT startup store_ready" in output
    assert "EVENT startup init_binance" in output
    assert "EVENT startup live_loop" in output
    assert "EVENT warmup_auto start" in output
    assert "EVENT warmup_auto done_3" in output

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "warmup_bars") == 3
        assert count_table(store, "bars") == 0
        assert count_table(store, "orders") == 0
        assert count_table(store, "fills") == 0
        assert count_table(store, "trades") == 0
        event_types = [
            row["event_type"]
            for row in store.connection.execute(
                "SELECT event_type FROM events ORDER BY event_id"
            ).fetchall()
        ]
        assert event_types.count("warmup_auto_before_live") == 2
    finally:
        store.close()


def test_live_paper_uses_existing_seed_without_rebuilding(tmp_path) -> None:
    config = small_live_config(tmp_path)
    warmup_qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 100.0),
                ("2026-06-18T08:43:00+08:00", 101.0),
                ("2026-06-18T08:44:00+08:00", 102.0),
            ]
        )
    )
    warmup_tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 20.0),
                ("2026-06-18T08:43:00+08:00", 20.5),
                ("2026-06-18T08:44:00+08:00", 21.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 30.0),
                ("2026-06-18T08:43:00+08:00", 30.0),
                ("2026-06-18T08:44:00+08:00", 30.0),
            ]
        )
    )
    WarmupRunner(
        config,
        qff_provider=warmup_qff,
        qff_fallback_provider=None,
        tsm_provider=warmup_tsm,
        usdttwd_provider=warmup_usd,
    ).run(reset_store=True, end=ts("2026-06-18T08:45:00+08:00"))

    live_qff = FakeQffProvider(pd.DataFrame())
    live_tsm = FakeOhlcvProvider(pd.DataFrame())
    live_usd = FakeOhlcvProvider(pd.DataFrame())
    terminal_output = io.StringIO()

    LivePaperRunner(
        config,
        qff_provider=live_qff,
        tsm_provider=live_tsm,
        usdttwd_provider=live_usd,
        clock=lambda: ts("2026-06-18T08:45:00+08:00"),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(resume=True, max_iterations=0)

    assert live_qff.fetch_1m_calls == []
    assert live_tsm.fetch_ohlcv_calls == []
    assert live_usd.fetch_ohlcv_calls == []
    assert "warmup_auto" not in terminal_output.getvalue()


def test_live_paper_skip_warmup_requires_existing_seed(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 100.0),
                ("2026-06-18T08:43:00+08:00", 101.0),
                ("2026-06-18T08:44:00+08:00", 102.0),
            ]
        )
    )
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 20.0),
                ("2026-06-18T08:43:00+08:00", 20.5),
                ("2026-06-18T08:44:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:42:00+08:00", 30.0),
                ("2026-06-18T08:43:00+08:00", 30.0),
                ("2026-06-18T08:44:00+08:00", 30.0),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="Warmup seed is missing"):
        LivePaperRunner(
            config,
            qff_provider=qff,
            tsm_provider=tsm,
            usdttwd_provider=usd,
            clock=lambda: ts("2026-06-18T08:45:00+08:00"),
            sleeper=lambda _: None,
        ).run(reset_store=True, max_iterations=0, skip_warmup=True)

    assert qff.fetch_1m_calls == []
    assert tsm.fetch_ohlcv_calls == []
    assert usd.fetch_ohlcv_calls == []


def test_live_paper_rejects_live_order_flag(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        safety=SafetyConfig(
            allow_live_order=True,
            validate_expected_zscore=False,
            expected_zscore_tolerance=1e-7,
        ),
    )

    with pytest.raises(RuntimeError, match="allow_live_order"):
        LivePaperRunner(config).run(max_iterations=0)


def test_warmup_runner_rejects_live_order_flag_before_provider_calls(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        safety=SafetyConfig(
            allow_live_order=True,
            validate_expected_zscore=False,
            expected_zscore_tolerance=1e-7,
        ),
    )
    qff = FakeQffProvider(rows([("2026-06-18T08:45:00+08:00", 100.0)]))
    tsm = FakeOhlcvProvider(rows([("2026-06-18T08:45:00+08:00", 20.0)]))
    usd = FakeOhlcvProvider(rows([("2026-06-18T08:45:00+08:00", 30.0)]))

    with pytest.raises(RuntimeError, match="allow_live_order"):
        WarmupRunner(
            config,
            qff_provider=qff,
            qff_fallback_provider=None,
            tsm_provider=tsm,
            usdttwd_provider=usd,
        ).run(reset_store=True)

    assert qff.select_calls == 0
    assert qff.fetch_1m_calls == []
    assert tsm.fetch_ohlcv_calls == []
    assert usd.fetch_ohlcv_calls == []


def test_warmup_runner_fixed_symbol_skips_front_month_selector_and_writes_seed_only(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, live=replace(config.live, qff_symbol="QFF202607"))
    qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 100.0),
                ("2026-06-18T08:46:00+08:00", 101.0),
                ("2026-06-18T08:47:00+08:00", 102.0),
            ]
        )
    )
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 20.0),
                ("2026-06-18T08:46:00+08:00", 20.5),
                ("2026-06-18T08:47:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T08:45:00+08:00", 30.0),
                ("2026-06-18T08:46:00+08:00", 30.0),
                ("2026-06-18T08:47:00+08:00", 30.0),
            ]
        )
    )

    result = WarmupRunner(
        config,
        qff_provider=qff,
        qff_fallback_provider=None,
        tsm_provider=tsm,
        usdttwd_provider=usd,
    ).run(reset_store=True, end=ts("2026-06-18T08:48:00+08:00"))

    assert result.bars_written == 3
    assert result.qff_symbol == "QFF202607"
    assert qff.select_calls == 0
    assert qff.fetch_1m_calls[0][0] == "QFF202607"

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "warmup_bars") == 3
        assert count_table(store, "bars") == 0
        assert count_table(store, "orders") == 0
        assert count_table(store, "fills") == 0
        assert count_table(store, "trades") == 0
        assert len(store.load_indicator_seed_bars(3)) == 3
    finally:
        store.close()


def test_qff_warmup_check_runner_uses_fubon_and_taifex_only(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(rows([("2026-06-18T08:47:00+08:00", 103.0)]))
    taifex = FakeQffProvider(
        rows(
            [
                ("2026-06-18T08:44:00+08:00", 99.0),
                ("2026-06-18T08:45:00+08:00", 100.0),
                ("2026-06-18T08:46:00+08:00", 101.0),
            ]
        )
    )

    result = QffWarmupCheckRunner(
        config,
        qff_provider=qff,
        taifex_provider=taifex,
    ).run(output_csv="", end=ts("2026-06-18T08:48:00+08:00"))

    assert result.qff_symbol == "QFF202607"
    assert len(result.report.frame) == 3
    assert result.report.null_count == 0
    assert result.report.source_rows == {"taifex": 3, "fubon": 1}
    assert result.output_csv is None


def test_contract_switch_cancels_entry_pending_state(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(
        state=StrategyState.ENTRY_PENDING,
        candidate_direction=Direction.SHORT_TSM_LONG_QFF,
        candidate_idx=10,
        candidate_time=ts("2026-07-09T08:45:00+08:00"),
        candidate_zscore=2.1,
        trading_qff_symbol="QFFG6",
    )
    contract = QffContractResolution(
        symbol="QFFH6",
        expiry="2026-08-19",
        policy_state="active",
    )

    assert should_switch_contract_before_processing(state, contract)
    cancel_entry_pending_for_contract_switch(state)

    assert state.state == StrategyState.FLAT
    assert state.candidate_direction is None
    assert state.candidate_idx == -1
    assert config.contract_policy.min_business_days_to_expiry == 5


def test_live_decision_ignores_mid_entry_when_tradable_spread_does_not_cross(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(state=StrategyState.FLAT)
    snapshot = indicator_snapshot(zscore=2.3)
    tradable = TradableSpreadSnapshot(
        mid_spread=2.3,
        mid_zscore=2.3,
        short_spread=1.8,
        short_zscore=1.8,
        long_spread=-1.7,
        long_zscore=-1.7,
    )

    decision, decision_type, decision_zscore, missing_book = build_live_decision_snapshot(
        config,
        state,
        snapshot,
        tradable,
    )

    assert not decision.zscore_valid
    assert decision.zscore is None
    assert decision_type is None
    assert decision_zscore is None
    assert not missing_book


def test_live_decision_uses_short_spread_for_short_entry(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(state=StrategyState.FLAT)
    tradable = TradableSpreadSnapshot(
        mid_spread=1.7,
        mid_zscore=1.7,
        short_spread=2.2,
        short_zscore=2.2,
        long_spread=-1.5,
        long_zscore=-1.5,
    )

    decision, decision_type, decision_zscore, missing_book = build_live_decision_snapshot(
        config,
        state,
        indicator_snapshot(zscore=1.7),
        tradable,
    )

    assert decision.zscore_valid
    assert decision.zscore == pytest.approx(2.2)
    assert decision.spread == pytest.approx(2.2)
    assert decision_type == "shortSpread"
    assert decision_zscore == pytest.approx(2.2)
    assert not missing_book


def test_live_decision_uses_long_spread_for_long_entry(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(state=StrategyState.FLAT)
    tradable = TradableSpreadSnapshot(
        mid_spread=-1.7,
        mid_zscore=-1.7,
        short_spread=1.5,
        short_zscore=1.5,
        long_spread=-2.2,
        long_zscore=-2.2,
    )

    decision, decision_type, decision_zscore, missing_book = build_live_decision_snapshot(
        config,
        state,
        indicator_snapshot(zscore=-1.7),
        tradable,
    )

    assert decision.zscore_valid
    assert decision.zscore == pytest.approx(-2.2)
    assert decision.spread == pytest.approx(-2.2)
    assert decision_type == "longSpread"
    assert decision_zscore == pytest.approx(-2.2)
    assert not missing_book


def test_live_decision_uses_opposite_tradable_spread_for_exit(tmp_path) -> None:
    config = small_live_config(tmp_path)
    short_state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
    )
    long_state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.LONG_TSM_SHORT_QFF,
    )
    tradable = TradableSpreadSnapshot(
        mid_spread=0.5,
        mid_zscore=0.5,
        short_spread=0.7,
        short_zscore=0.7,
        long_spread=-0.3,
        long_zscore=-0.3,
    )

    short_decision, short_type, short_zscore, _ = build_live_decision_snapshot(
        config,
        short_state,
        indicator_snapshot(zscore=0.5),
        tradable,
    )
    long_decision, long_type, long_zscore, _ = build_live_decision_snapshot(
        config,
        long_state,
        indicator_snapshot(zscore=0.5),
        tradable,
    )

    assert short_decision.zscore == pytest.approx(-0.3)
    assert short_type == "longSpread"
    assert short_zscore == pytest.approx(-0.3)
    assert long_decision.zscore == pytest.approx(0.7)
    assert long_type == "shortSpread"
    assert long_zscore == pytest.approx(0.7)


def test_contract_switch_marks_open_position_as_pending_switch(tmp_path) -> None:
    state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
        trading_qff_symbol="QFFG6",
    )
    contract = QffContractResolution(
        symbol="QFFH6",
        expiry="2026-08-19",
        policy_state="active",
    )

    assert not should_switch_contract_before_processing(state, contract)
    mark_pending_contract_switch_if_needed(state, contract)

    assert state.pending_symbol_switch
    assert state.contract_policy_state == "pending_symbol_switch"
    assert state.eligible_active_qff_symbol == "QFFH6"


def test_contract_policy_force_exit_helper_uses_configured_deadline(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
        trading_qff_symbol="QFFG6",
        trading_qff_expiry="2026-07-15",
    )

    assert not should_force_exit_for_contract_policy(
        config,
        state,
        ts("2026-07-14T13:34:00+08:00"),
    )
    assert should_force_exit_for_contract_policy(
        config,
        state,
        ts("2026-07-14T13:35:00+08:00"),
    )
