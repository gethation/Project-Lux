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
    LiveMinuteBarBuilder,
    LiveQuote,
    LiveQuoteSet,
    TaifexQffTradeDownloader,
    WarmupBuilder,
    build_qff_warmup_source_report,
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
    cancel_entry_pending_for_contract_switch,
    mark_pending_contract_switch_if_needed,
    should_force_exit_for_contract_policy,
    should_switch_contract_before_processing,
)
from lux_trader.models import Direction, StrategyState
from lux_trader.strategy import StrategyRuntimeState
from lux_trader.store import SQLiteStore

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


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def rows(values: list[tuple[str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"timestamp": [ts(timestamp) for timestamp, _ in values], "close": [value for _, value in values]}
    )


def quote(source: str, timestamp: str, price: float) -> LiveQuote:
    return LiveQuote(source=source, symbol=source, timestamp=ts(timestamp), price=price)


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
            quote("qff", "2026-06-18T08:45:59+08:00", 100.0),
            quote("qff", "2026-06-18T08:46:00+08:00", 100.0),
            quote("qff", "2026-06-18T08:46:01+08:00", 100.0),
        ]
    )
    tsm = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("tsm", "2026-06-18T08:45:59+08:00", 20.0),
            quote("tsm", "2026-06-18T08:46:00+08:00", 20.0),
            quote("tsm", "2026-06-18T08:46:01+08:00", 20.0),
        ],
    )
    usd = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("usd", "2026-06-18T08:45:59+08:00", 30.0),
            quote("usd", "2026-06-18T08:46:00+08:00", 30.0),
            quote("usd", "2026-06-18T08:46:01+08:00", 30.0),
        ],
    )

    result = LivePaperRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=lambda: next(clocks),
        sleeper=lambda _: None,
    ).run(resume=True, max_iterations=3)

    assert result.bars_processed == 1
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        summary = store.build_summary(config.strategy, config.fees)
        assert summary["rows"] == 1
        assert summary["trade_count"] == 0
    finally:
        store.close()


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
