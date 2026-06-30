from __future__ import annotations

import io
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import pytest

import lux_trader.runtime.live.engine as live_engine
from lux_trader.config import AppConfig, LiveMarketDataConfig, SafetyConfig, load_config
from lux_trader.execution import (
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    order_request_from_execution_leg,
)
from lux_trader.execution.intent import PairExecutionPlan
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.integrations.ccxt_market_data import CcxtTickerMarketData
from lux_trader.integrations.fubon.market_data import (
    FubonQffMarketData,
    parse_fubon_books_quote,
)
from lux_trader.integrations.taifex.downloader import (
    TaifexQffTradeDownloader,
    parse_taifex_download_entries,
)
from lux_trader.market_data import (
    LiveMinuteBarBuilder,
    LiveQuote,
    LiveQuoteSet,
    WarmupBuilder,
    build_qff_warmup_source_report,
    parse_timestamp,
    qff_symbol_to_taifex_contract_month,
    select_qff_front_month,
)
from lux_trader.runtime.live import LiveDryRunRunner, LiveExecuteRunner, LivePaperRunner
from lux_trader.runtime.live.bootstrap import (
    WindowsTimeSyncResult,
    run_live_startup_preflight,
)
from lux_trader.runtime.live.contracts import (
    QffContractResolution,
    cancel_entry_pending_for_contract_switch,
    mark_pending_contract_switch_if_needed,
    should_force_exit_for_contract_policy,
    should_switch_contract_before_processing,
)
from lux_trader.runtime.live.engine import build_live_decision_snapshot
from lux_trader.runtime.live.warmup import (
    QffWarmupCheckRunner,
    WarmupRunner,
)
from lux_trader.core.models import (
    BrokerName,
    Direction,
    Fill,
    IndicatorSnapshot,
    MarketBar,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from lux_trader.reconciliation import BrokerAccountSnapshot, BrokerPositionSnapshot
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.store import SQLiteStore
from lux_trader.terminal_ui import LiveTerminalReporter
from lux_trader.core.tradable_spread import TradableSpreadSnapshot

from conftest import make_app_config


class FakeQffProvider:
    def __init__(
        self,
        rows: pd.DataFrame | None = None,
        quotes: list[LiveQuote | Exception] | None = None,
    ) -> None:
        self.rows = rows if rows is not None else pd.DataFrame(columns=["timestamp", "close"])
        self.quotes = list(quotes or [])
        self.select_calls = 0
        self.fetch_1m_calls: list[tuple[str, datetime, datetime]] = []
        self.quote_calls: list[str] = []
        self.teardown_books_calls = 0
        self.restart_books_calls: list[str] = []

    def select_front_month_symbol(self, product: str) -> str:
        self.select_calls += 1
        return "QFF202607"

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.fetch_1m_calls.append((symbol, start, end))
        return self.rows.copy()

    def fetch_quote(self, symbol: str) -> LiveQuote:
        self.quote_calls.append(symbol)
        if not self.quotes:
            raise RuntimeError("No fake QFF quotes left")
        quote = self.quotes.pop(0)
        if isinstance(quote, Exception):
            raise quote
        return quote

    def teardown_books_session(self) -> None:
        self.teardown_books_calls += 1

    def restart_books_session(self, symbol: str, *, after_hours: bool | None = None) -> None:
        self.restart_books_calls.append(symbol)


class FakeOhlcvProvider:
    def __init__(
        self,
        rows: pd.DataFrame,
        quotes: list[LiveQuote | Exception] | None = None,
    ) -> None:
        self.rows = rows
        self.quotes = list(quotes or [])
        self.fetch_ohlcv_calls: list[tuple[str, datetime, datetime]] = []
        self.quote_calls: list[str] = []

    def fetch_ohlcv_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.fetch_ohlcv_calls.append((symbol, start, end))
        return self.rows.copy()

    def fetch_quote(self, symbol: str) -> LiveQuote:
        self.quote_calls.append(symbol)
        if not self.quotes:
            raise RuntimeError("No fake quotes left")
        quote = self.quotes.pop(0)
        if isinstance(quote, Exception):
            raise quote
        return quote


class FakeLiveExecutionAdapter:
    def __init__(self, broker: BrokerName) -> None:
        self.broker = broker
        self.plans: list[PairExecutionPlan] = []

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        self.plans.append(plan)
        leg = plan.legs[0]
        order = OrderResult(
            order_id=f"LIVE-FAKE-{self.broker.value}-{len(self.plans)}",
            request=order_request_from_execution_leg(leg),
            status=OrderStatus.FILLED,
        )
        fill = Fill(
            fill_id=f"LIVE-FAKE-FILL-{self.broker.value}-{len(self.plans)}",
            order_id=order.order_id,
            broker=leg.broker,
            symbol=leg.symbol,
            side=leg.side,
            quantity=leg.quantity,
            price=leg.expected_price or leg.price,
            fee_twd=leg.fee_twd,
            timestamp=leg.timestamp,
            row_index=leg.row_index,
            qff_symbol=leg.qff_symbol,
            qff_expiry=leg.qff_expiry,
            contract_policy_state=leg.contract_policy_state,
        )
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=ExecutionOutcomeStatus.FILLED,
            message=f"{self.broker.value} fake live fill",
            orders=(order,),
            fills=(fill,),
            payload={"adapter": "fake_live_execution"},
        )


class FixedPositionReadOnlyBroker:
    def __init__(
        self,
        *,
        broker: BrokerName,
        symbol: str,
        quantity: float,
        fetched_at: datetime,
    ) -> None:
        self.broker = broker
        self.symbol = symbol
        self.quantity = quantity
        self.fetched_at = fetched_at

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id=f"{self.broker.value}-FAKE",
            fetched_at=self.fetched_at,
            positions=(
                BrokerPositionSnapshot(
                    broker=self.broker,
                    symbol=self.symbol,
                    quantity=self.quantity,
                ),
            ),
        )

    def close(self) -> None:
        return None


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
        fail_order_books: list[Exception] | None = None,
    ) -> None:
        self.order_book = order_book
        self.ticker = ticker or {"last": 20.0, "timestamp": 1781743501000}
        self.fail_order_books = list(fail_order_books or [])
        if fail_first_order_book is not None:
            self.fail_order_books.insert(0, fail_first_order_book)
        self.fetch_order_book_calls: list[tuple[str, int | None]] = []
        self.fetch_ticker_calls: list[str] = []

    def fetch_order_book(self, symbol: str, limit: int | None = None) -> dict[str, object]:
        self.fetch_order_book_calls.append((symbol, limit))
        if self.fail_order_books:
            raise self.fail_order_books.pop(0)
        return self.order_book

    def fetch_ticker(self, symbol: str) -> dict[str, object]:
        self.fetch_ticker_calls.append(symbol)
        return self.ticker


class FakeFubonWebSocket:
    def __init__(self) -> None:
        self.listeners: dict[str, object] = {}
        self.connected = False
        self.connect_calls = 0
        self.subscriptions: list[dict[str, object]] = []
        self.unsubscriptions: list[dict[str, object]] = []
        self.disconnected = False

    def on(self, event: str, listener: object) -> None:
        self.listeners[event] = listener

    def connect(self) -> None:
        self.connected = True
        self.connect_calls += 1

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
            qff_book_stale_seconds=55.0,
            sync_windows_time_on_startup=True,
            clock_skew_fail_seconds=60.0,
            windows_time_sync_timeout_seconds=15.0,
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


def write_minimal_config(tmp_path: Path, live_body: str = "") -> Path:
    config_path = tmp_path / "config.test.toml"
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                "store_path = 'project_lux.sqlite3'",
                "input_csv = ''",
                "",
                "[live_market_data]",
                live_body,
            ]
        ),
        encoding="utf-8",
    )
    return config_path


def test_load_config_defaults_live_freshness_and_clock_preflight(tmp_path) -> None:
    config = load_config(write_minimal_config(tmp_path))

    assert config.live.stale_seconds == pytest.approx(10.0)
    assert config.live.qff_book_stale_seconds == pytest.approx(55.0)
    assert config.live.sync_windows_time_on_startup is True
    assert config.live.clock_skew_fail_seconds == pytest.approx(60.0)
    assert config.live.windows_time_sync_timeout_seconds == pytest.approx(15.0)


def test_project_config_relative_paths_resolve_from_project_root() -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = load_config(project_root / "configs" / "live.example.toml")

    assert config.store_path == project_root / "data" / "project_lux_live.sqlite3"
    assert config.live.fubon_env_path == project_root / ".env"
    assert config.live.taifex_cache_dir == project_root / "data" / "taifex_cache"


def test_load_config_reads_live_freshness_and_clock_preflight(tmp_path) -> None:
    config = load_config(
        write_minimal_config(
            tmp_path,
            "\n".join(
                [
                    "stale_seconds = 10.0",
                    "qff_book_stale_seconds = 42.5",
                    "sync_windows_time_on_startup = false",
                    "clock_skew_fail_seconds = 12.5",
                    "windows_time_sync_timeout_seconds = 3.0",
                ]
            ),
        )
    )

    assert config.live.stale_seconds == pytest.approx(10.0)
    assert config.live.qff_book_stale_seconds == pytest.approx(42.5)
    assert config.live.sync_windows_time_on_startup is False
    assert config.live.clock_skew_fail_seconds == pytest.approx(12.5)
    assert config.live.windows_time_sync_timeout_seconds == pytest.approx(3.0)


def test_live_startup_preflight_syncs_windows_time_and_accepts_clock_skew(tmp_path) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()
    sync_timeouts: list[float] = []
    market_symbols: list[str] = []

    def sync_runner(timeout_seconds: float) -> WindowsTimeSyncResult:
        sync_timeouts.append(timeout_seconds)
        return WindowsTimeSyncResult(True, "ok")

    def market_time_probe(symbol: str) -> datetime:
        market_symbols.append(symbol)
        return ts("2026-06-23T11:45:00+08:00")

    run_live_startup_preflight(
        config,
        LiveTerminalReporter(terminal_output, color=False),
        lambda: ts("2026-06-23T11:45:00+08:00"),
        platform_name="win32",
        sync_runner=sync_runner,
        market_time_probe=market_time_probe,
    )

    assert sync_timeouts == [15.0]
    assert market_symbols == ["TSM/USDT:USDT"]
    output = terminal_output.getvalue()
    assert "EVENT startup sync_windows_time" in output
    assert "EVENT startup clock_ok skew=0.000s" in output


def test_live_startup_preflight_warns_on_sync_failure_but_allows_good_skew(
    tmp_path,
) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()

    run_live_startup_preflight(
        config,
        LiveTerminalReporter(terminal_output, color=False),
        lambda: ts("2026-06-23T11:45:00+08:00"),
        platform_name="win32",
        sync_runner=lambda _: WindowsTimeSyncResult(False, "exit_1"),
        market_time_probe=lambda _: ts("2026-06-23T11:44:59+08:00"),
    )

    output = terminal_output.getvalue()
    assert "WARN windows_time_sync resync_failed:exit_1" in output
    assert "EVENT startup clock_ok skew=1.000s" in output


def test_live_startup_preflight_skips_windows_sync_on_non_windows(tmp_path) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()

    def sync_runner(_: float) -> WindowsTimeSyncResult:
        raise AssertionError("sync should be skipped")

    run_live_startup_preflight(
        config,
        LiveTerminalReporter(terminal_output, color=False),
        lambda: ts("2026-06-23T11:45:00+08:00"),
        platform_name="linux",
        sync_runner=sync_runner,
        market_time_probe=lambda _: ts("2026-06-23T11:45:00+08:00"),
    )

    output = terminal_output.getvalue()
    assert "sync_windows_time" not in output
    assert "EVENT startup clock_ok skew=0.000s" in output


def test_live_startup_preflight_rejects_bad_clock_skew(tmp_path) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()

    with pytest.raises(RuntimeError, match="Clock skew exceeds limit"):
        run_live_startup_preflight(
            config,
            LiveTerminalReporter(terminal_output, color=False),
            lambda: ts("2026-06-23T03:45:00+08:00"),
            platform_name="win32",
            sync_runner=lambda _: WindowsTimeSyncResult(True, "ok"),
            market_time_probe=lambda _: ts("2026-06-23T11:45:00+08:00"),
        )

    output = terminal_output.getvalue()
    assert "EVENT startup sync_windows_time" in output
    assert "ERR clock_skew local=2026-06-23T03:45:00+08:00" in output


def test_live_startup_preflight_rejects_unavailable_market_time(tmp_path) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()

    def market_time_probe(_: str) -> datetime:
        raise RuntimeError("market down")

    with pytest.raises(RuntimeError, match="Unable to verify market clock skew"):
        run_live_startup_preflight(
            config,
            LiveTerminalReporter(terminal_output, color=False),
            lambda: ts("2026-06-23T11:45:00+08:00"),
            platform_name="win32",
            sync_runner=lambda _: WindowsTimeSyncResult(True, "ok"),
            market_time_probe=market_time_probe,
        )

    assert "ERR clock_skew unavailable:RuntimeError" in terminal_output.getvalue()


def test_live_runtime_clock_preflight_failure_stops_before_qff_provider(
    tmp_path,
    monkeypatch,
) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(pd.DataFrame())
    tsm = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())

    def fail_preflight(*_: object, **__: object) -> None:
        raise RuntimeError("clock skew test")

    monkeypatch.setattr(live_engine, "run_live_startup_preflight", fail_preflight)

    with pytest.raises(RuntimeError, match="clock skew test"):
        LivePaperRunner(
            config,
            qff_provider=qff,
            tsm_provider=tsm,
            usdttwd_provider=usd,
            sleeper=lambda _: None,
        ).run(max_iterations=0)

    assert qff.select_calls == 0
    assert qff.fetch_1m_calls == []
    assert qff.quote_calls == []
    assert qff.restart_books_calls == []


def test_live_runtime_skips_clock_preflight_when_clock_is_injected(
    tmp_path,
    monkeypatch,
) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    qff = FakeQffProvider(pd.DataFrame())
    tsm = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())

    def fail_preflight(*_: object, **__: object) -> None:
        raise AssertionError("preflight should be skipped")

    monkeypatch.setattr(live_engine, "run_live_startup_preflight", fail_preflight)

    result = LivePaperRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=lambda: ts("2026-06-23T08:45:00+08:00"),
        sleeper=lambda _: None,
    ).run(max_iterations=0, skip_warmup=True)

    assert result.iterations == 0
    assert qff.select_calls == 1


def count_table(store: SQLiteStore, table: str) -> int:
    row = store.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def dry_run_warmup_rows() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 100.0),
                ("2026-06-18T04:59:00+08:00", 100.0),
                ("2026-06-18T05:00:00+08:00", 100.0),
            ]
        ),
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 20.0),
                ("2026-06-18T04:59:00+08:00", 20.0),
                ("2026-06-18T05:00:00+08:00", 20.0),
            ]
        ),
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 25.0),
                ("2026-06-18T04:59:00+08:00", 25.0),
                ("2026-06-18T05:00:00+08:00", 25.0),
            ]
        ),
    )


def seed_warmup_bars(config: AppConfig) -> None:
    bars = [
        MarketBar(
            row_index=index,
            timestamp=ts(f"2026-06-18T08:4{index}:00+08:00"),
            qff_close=100.0,
            qff_close_filled=100.0,
            tsm_twd_fair=100.0 + index,
            spread=float(index),
            qff_symbol="QFF202607",
            qff_expiry="2026-07-15",
            contract_policy_state="active",
        )
        for index in range(config.strategy.zscore_window)
    ]
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        store.replace_warmup_bars(bars)
        store.commit()
    finally:
        store.close()


def dry_run_quote_providers(
    quote_times: list[str],
    *,
    qff_rows: pd.DataFrame | None = None,
    tsm_rows: pd.DataFrame | None = None,
    usd_rows: pd.DataFrame | None = None,
    qff_price: float = 100.0,
    tsm_price: float = 20.0,
    usd_price: float = 30.0,
) -> tuple[FakeQffProvider, FakeOhlcvProvider, FakeOhlcvProvider]:
    default_qff_rows, default_tsm_rows, default_usd_rows = dry_run_warmup_rows()
    return (
        FakeQffProvider(
            qff_rows if qff_rows is not None else default_qff_rows,
            quotes=[
                quote("qff", value, qff_price, bid=qff_price - 0.1, ask=qff_price + 0.1)
                for value in quote_times
            ],
        ),
        FakeOhlcvProvider(
            tsm_rows if tsm_rows is not None else default_tsm_rows,
            quotes=[
                quote("tsm", value, tsm_price, bid=tsm_price - 0.01, ask=tsm_price + 0.01)
                for value in quote_times
            ],
        ),
        FakeOhlcvProvider(
            usd_rows if usd_rows is not None else default_usd_rows,
            quotes=[
                quote("usd", value, usd_price, bid=usd_price - 0.01, ask=usd_price + 0.01)
                for value in quote_times
            ],
        ),
    )


def dry_run_clock(values: list[str]):
    clocks = iter(ts(value) for value in values)
    return lambda: next(clocks)


def test_live_dry_run_closed_calendar_skips_market_data_and_bars(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        trading_calendar=replace(
            config.trading_calendar,
            closed_dates=(date(2026, 6, 19),),
        ),
    )
    seed_warmup_bars(config)
    qff = FakeQffProvider(pd.DataFrame())
    tsm = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-20T02:30:00+08:00",
                "2026-06-20T02:30:01+08:00",
                "2026-06-20T02:30:02+08:00",
                "2026-06-20T02:30:03+08:00",
                "2026-06-20T02:30:04+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=3, skip_warmup=True)

    assert result.iterations == 3
    assert result.bars_processed == 0
    assert result.plans_recorded == 0
    assert qff.quote_calls == []
    assert tsm.quote_calls == []
    assert usd.quote_calls == []
    output = terminal_output.getvalue()
    assert "LIVE non-trading session next=06/22 08:45" in output
    assert "BAR" not in output

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "market_ticks") == 0
        assert count_table(store, "bars") == 0
        assert count_table(store, "execution_plans") == 0
        non_trading_events = store.connection.execute(
            """
            SELECT COUNT(*)
            FROM events
            WHERE event_type = 'non_trading_session'
            """
        ).fetchone()[0]
        assert non_trading_events == 1
    finally:
        store.close()


def test_live_runtime_tears_down_qff_books_during_non_trading_and_restarts_on_open(
    tmp_path,
) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-23T04:58:01+08:00",
            "2026-06-23T08:45:00+08:00",
        ]
    )

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T04:58:00+08:00",
                "2026-06-23T04:58:01+08:00",
                "2026-06-23T05:01:00+08:00",
                "2026-06-23T08:45:00+08:00",
                "2026-06-23T08:45:01+08:00",
            ]
        ),
        sleeper=lambda _: None,
    ).run(max_iterations=3, skip_warmup=True)

    assert result.iterations == 3
    assert qff.teardown_books_calls == 1
    assert qff.restart_books_calls == ["QFF202607"]
    assert qff.quote_calls == ["QFF202607", "QFF202607"]


def test_live_runtime_qff_watchdog_restarts_once_with_backoff(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    qff_rows, tsm_rows, usd_rows = dry_run_warmup_rows()
    qff = FakeQffProvider(
        qff_rows,
        quotes=[
            quote("qff", "2026-06-23T08:40:00+08:00", 100.0, bid=99.9, ask=100.1),
            quote("qff", "2026-06-23T08:40:00+08:00", 100.0, bid=99.9, ask=100.1),
            quote("qff", "2026-06-23T08:40:00+08:00", 100.0, bid=99.9, ask=100.1),
        ],
    )
    tsm = FakeOhlcvProvider(
        tsm_rows,
        quotes=[
            quote("tsm", "2026-06-23T08:45:01+08:00", 20.0, bid=19.99, ask=20.01),
            quote("tsm", "2026-06-23T08:45:11+08:00", 20.0, bid=19.99, ask=20.01),
            quote("tsm", "2026-06-23T08:45:20+08:00", 20.0, bid=19.99, ask=20.01),
        ],
    )
    usd = FakeOhlcvProvider(
        usd_rows,
        quotes=[
            quote("usd", "2026-06-23T08:45:01+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-23T08:45:11+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-23T08:45:20+08:00", 30.0, bid=29.99, ask=30.01),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T08:45:00+08:00",
                "2026-06-23T08:45:01+08:00",
                "2026-06-23T08:45:11+08:00",
                "2026-06-23T08:45:20+08:00",
                "2026-06-23T08:45:21+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=3, skip_warmup=True)

    assert result.iterations == 3
    assert qff.restart_books_calls == ["QFF202607"]
    assert "WARN qff_reconnecting skip_signal" in terminal_output.getvalue()


def test_live_runtime_uses_cached_quote_after_transient_fetch_failure(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    qff, _, usd = dry_run_quote_providers(
        [
            "2026-06-23T08:45:00+08:00",
            "2026-06-23T08:45:01+08:00",
        ]
    )
    tsm = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("tsm", "2026-06-23T08:45:00+08:00", 20.0, bid=19.99, ask=20.01),
            RuntimeError("request timeout"),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T08:45:00+08:00",
                "2026-06-23T08:45:00+08:00",
                "2026-06-23T08:45:01+08:00",
                "2026-06-23T08:45:01+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=2, skip_warmup=True)

    assert result.iterations == 2
    assert "WARN fetch_tsm failed:RuntimeError" in terminal_output.getvalue()
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "market_ticks") == 6
    finally:
        store.close()


def test_live_runtime_skips_iteration_when_fetch_fails_without_cached_quote(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    qff = FakeQffProvider(
        pd.DataFrame(),
        quotes=[
            quote("qff", "2026-06-23T08:45:00+08:00", 100.0, bid=99.9, ask=100.1),
        ],
    )
    tsm = FakeOhlcvProvider(pd.DataFrame(), quotes=[RuntimeError("request timeout")])
    usd = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("usd", "2026-06-23T08:45:00+08:00", 30.0, bid=29.99, ask=30.01),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=lambda: ts("2026-06-23T08:45:00+08:00"),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=1, skip_warmup=True)

    assert result.iterations == 1
    output = terminal_output.getvalue()
    assert "WARN fetch_tsm failed:RuntimeError" in output
    assert "WARN market_data_fetch skip_iteration" in output
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "market_ticks") == 0
    finally:
        store.close()


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


def test_ccxt_quote_falls_back_to_ticker_when_supported_depth_retry_fails() -> None:
    provider = object.__new__(CcxtTickerMarketData)
    provider.exchange_id = "binanceusdm"
    provider.exchange = FakeCcxtExchange(
        order_book={},
        ticker={"last": 459.65, "timestamp": 1781743501000},
        fail_order_books=[
            RuntimeError(
                'binanceusdm {"code":-4021,"msg":"1 is not valid depth limit"}'
            ),
            TimeoutError("read timeout"),
        ],
    )

    fetched = provider.fetch_quote("TSM/USDT:USDT")

    assert fetched.price == pytest.approx(459.65)
    assert fetched.bid is None
    assert fetched.ask is None
    assert fetched.raw is not None
    assert fetched.raw["book_limit_used"] == 5
    assert "retry_limit_5:TimeoutError" in fetched.raw["book_error"]
    assert provider.exchange.fetch_order_book_calls == [
        ("TSM/USDT:USDT", 1),
        ("TSM/USDT:USDT", 5),
    ]
    assert provider.exchange.fetch_ticker_calls == ["TSM/USDT:USDT"]


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


def test_fubon_books_restart_clears_cache_disconnects_and_resubscribes() -> None:
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

    provider.restart_books_session("QFFG6", after_hours=False)

    assert websocket.disconnected
    assert websocket.connect_calls == 2
    assert websocket.subscriptions == [
        {"channel": "books", "symbol": "QFFG6", "afterHours": True},
        {"channel": "books", "symbol": "QFFG6", "afterHours": False},
    ]
    assert provider._latest_books == {}
    assert provider._book_subscribed_symbols == {"QFFG6"}


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


def test_warmup_builder_refuses_when_forward_fill_ratio_too_high(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        live=replace(config.live, warmup_forward_fill_max_ratio=0.2),
    )
    # Same data as the success case (1 of 3 minutes forward-filled = 0.33 > 0.2).
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

    with pytest.raises(RuntimeError, match="forward-fill ratio"):
        WarmupBuilder(
            live_config=config.live,
            qff_intraday_provider=intraday,
            qff_fallback_provider=fallback,
            tsm_provider=tsm,
            usdttwd_provider=usd,
        ).build(qff_symbol="QFF202607", end=ts("2026-06-18T08:48:42+08:00"))


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
                ("2026-06-18T08:45:00+08:00", 99.0),
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

    assert bars[0].qff_close == 99.0
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

    with pytest.raises(RuntimeError, match="QFF session warmup has only"):
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
                ("2026-06-18T04:58:00+08:00", 100.0),
                ("2026-06-18T04:59:00+08:00", 100.0),
                ("2026-06-18T05:00:00+08:00", 100.0),
            ]
        )
    )
    warmup_tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 20.0),
                ("2026-06-18T04:59:00+08:00", 20.0),
                ("2026-06-18T05:00:00+08:00", 20.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 30.0),
                ("2026-06-18T04:59:00+08:00", 30.0),
                ("2026-06-18T05:00:00+08:00", 30.0),
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
                ("2026-06-18T04:58:00+08:00", 100.0),
                ("2026-06-18T04:59:00+08:00", 100.0),
                ("2026-06-18T05:00:00+08:00", 100.0),
            ]
        )
    )
    warmup_tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 20.0),
                ("2026-06-18T04:59:00+08:00", 20.0),
                ("2026-06-18T05:00:00+08:00", 20.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 30.0),
                ("2026-06-18T04:59:00+08:00", 30.0),
                ("2026-06-18T05:00:00+08:00", 30.0),
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


def test_live_dry_run_records_simulated_entry_and_opens_position(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, strategy=replace(config.strategy, entry_z=1.0))
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T08:45:30+08:00",
            "2026-06-18T08:45:59+08:00",
            "2026-06-18T08:46:01+08:00",
            "2026-06-18T08:46:59+08:00",
            "2026-06-18T08:47:01+08:00",
        ]
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T08:45:00+08:00",
                "2026-06-18T08:45:30+08:00",
                "2026-06-18T08:45:59+08:00",
                "2026-06-18T08:46:01+08:00",
                "2026-06-18T08:46:59+08:00",
                "2026-06-18T08:47:01+08:00",
                "2026-06-18T08:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(reset_store=True, max_iterations=5)

    assert result.bars_processed == 2
    assert result.plans_recorded == 1
    output = terminal_output.getvalue()
    assert "OPEN entry_fill" in output
    assert "ENTRY_PENDING entry_signal" not in output
    assert "EVENT entry_signal zscore_crossed" in output
    assert "EVENT entry_fill dry_run_filled" in output
    assert "EVENT dry_run execution_filled" in output

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "execution_plans") == 1
        assert count_table(store, "execution_outcomes") == 1
        assert count_table(store, "execution_legs") == 2
        assert count_table(store, "orders") == 2
        assert count_table(store, "fills") == 2
        assert count_table(store, "trades") == 0
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.OPEN
        assert state.strategy.position_direction is not None
        plan = store.load_latest_execution_plan_payload()
        assert plan is not None
        assert plan["status"] == "recorded"
        assert plan["plan_type"] == "entry"
        assert plan["price_policy"] == "live_touch_market"
        assert plan["order_type"] == "market"
        assert plan["max_plan_age_seconds"] == config.live_execution.max_plan_age_seconds
        expected_prices = sorted(leg["expected_price"] for leg in plan["legs"])
        assert all(leg["order_type"] == "market" for leg in plan["legs"])
        assert all(leg["trigger_bid"] is not None for leg in plan["legs"])
        assert all(leg["trigger_ask"] is not None for leg in plan["legs"])
        assert all(leg["price"] == leg["expected_price"] for leg in plan["legs"])
        fill_prices = sorted(
            row["price"]
            for row in store.connection.execute(
                "SELECT price FROM fills ORDER BY price"
            ).fetchall()
        )
        assert fill_prices == expected_prices
        order_ids = [
            row["order_id"]
            for row in store.connection.execute(
                "SELECT order_id FROM orders ORDER BY order_id"
            ).fetchall()
        ]
        assert all(order_id.startswith("DRYRUN-") for order_id in order_ids)
    finally:
        store.close()


def test_live_execute_uses_shared_runtime_and_real_adapter_pipeline(
    tmp_path,
    monkeypatch,
) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        safety=replace(config.safety, allow_live_order=True),
        strategy=replace(config.strategy, entry_z=1.0),
        live_execution=replace(config.live_execution, enabled=True),
    )
    for name in (
        "PROJECT_LUX_ALLOW_LIVE_ORDER",
        "FUBON_ALLOW_LIVE_ORDER",
        "BINANCE_ALLOW_LIVE_ORDER",
    ):
        monkeypatch.setenv(name, "1")
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T08:45:30+08:00",
            "2026-06-18T08:45:59+08:00",
            "2026-06-18T08:46:01+08:00",
            "2026-06-18T08:46:59+08:00",
            "2026-06-18T08:47:01+08:00",
        ]
    )
    qff_adapter = FakeLiveExecutionAdapter(BrokerName.FUBON_QFF)
    binance_adapter = FakeLiveExecutionAdapter(BrokerName.BINANCE_TSM)
    terminal_output = io.StringIO()

    result = LiveExecuteRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        fubon_adapter=qff_adapter,
        binance_adapter=binance_adapter,
        readonly_brokers=(
            FixedPositionReadOnlyBroker(
                broker=BrokerName.FUBON_QFF,
                symbol="QFF202607",
                quantity=100.0,
                fetched_at=ts("2026-06-18T08:47:01+08:00"),
            ),
            FixedPositionReadOnlyBroker(
                broker=BrokerName.BINANCE_TSM,
                symbol="TSM/USDT:USDT",
                quantity=-(1_000_000.0 / 120.0),
                fetched_at=ts("2026-06-18T08:47:01+08:00"),
            ),
        ),
        clock=dry_run_clock(
            [
                "2026-06-18T08:45:00+08:00",
                "2026-06-18T08:45:30+08:00",
                "2026-06-18T08:45:59+08:00",
                "2026-06-18T08:46:01+08:00",
                "2026-06-18T08:46:59+08:00",
                "2026-06-18T08:47:01+08:00",
                "2026-06-18T08:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(reset_store=True, max_iterations=5)

    assert result.bars_processed == 2
    assert result.plans_recorded == 1
    assert len(qff_adapter.plans) == 1
    assert len(binance_adapter.plans) == 1
    output = terminal_output.getvalue()
    assert "EVENT warmup_auto start" in output
    assert "EVENT live_execution filled" in output
    assert "EVENT post_trade_reconciliation matched" in output
    assert "OPEN entry_fill" in output

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "warmup_bars") == config.live.warmup_minutes
        assert count_table(store, "market_ticks") > 0
        assert count_table(store, "execution_plans") == 1
        assert count_table(store, "execution_outcomes") == 1
        assert count_table(store, "execution_legs") == 2
        assert count_table(store, "orders") == 2
        assert count_table(store, "fills") == 2
        assert count_table(store, "broker_reconciliation_runs") == 1
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.OPEN
        assert state.strategy.position_direction == Direction.SHORT_TSM_LONG_QFF
        report = store.load_latest_reconciliation_report()
        assert report is not None
        assert report.status.value == "matched"
        plan = store.load_latest_execution_plan_payload()
        assert plan is not None
        assert plan["reason"] == "live_entry_order"
        assert plan["price_policy"] == "live_touch_market"
        assert plan["order_type"] == "market"
    finally:
        store.close()


def _live_execute_resume_brokers(*, qff_quantity: float, tsm_quantity: float, at: str):
    return (
        FixedPositionReadOnlyBroker(
            broker=BrokerName.FUBON_QFF,
            symbol="QFF202607",
            quantity=qff_quantity,
            fetched_at=ts(at),
        ),
        FixedPositionReadOnlyBroker(
            broker=BrokerName.BINANCE_TSM,
            symbol="TSM/USDT:USDT",
            quantity=tsm_quantity,
            fetched_at=ts(at),
        ),
    )


def _run_live_execute_entry_to_open(config) -> None:
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T08:45:30+08:00",
            "2026-06-18T08:45:59+08:00",
            "2026-06-18T08:46:01+08:00",
            "2026-06-18T08:46:59+08:00",
            "2026-06-18T08:47:01+08:00",
        ]
    )
    LiveExecuteRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        fubon_adapter=FakeLiveExecutionAdapter(BrokerName.FUBON_QFF),
        binance_adapter=FakeLiveExecutionAdapter(BrokerName.BINANCE_TSM),
        readonly_brokers=_live_execute_resume_brokers(
            qff_quantity=100.0,
            tsm_quantity=-(1_000_000.0 / 120.0),
            at="2026-06-18T08:47:01+08:00",
        ),
        clock=dry_run_clock(
            [
                "2026-06-18T08:45:00+08:00",
                "2026-06-18T08:45:30+08:00",
                "2026-06-18T08:45:59+08:00",
                "2026-06-18T08:46:01+08:00",
                "2026-06-18T08:46:59+08:00",
                "2026-06-18T08:47:01+08:00",
                "2026-06-18T08:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(reset_store=True, max_iterations=5)


def _resume_live_execute(config, *, readonly):
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T08:47:30+08:00",
            "2026-06-18T08:47:59+08:00",
            "2026-06-18T08:48:01+08:00",
        ],
        qff_rows=pd.DataFrame(),
        tsm_rows=pd.DataFrame(),
        usd_rows=pd.DataFrame(),
    )
    terminal = io.StringIO()
    LiveExecuteRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        fubon_adapter=FakeLiveExecutionAdapter(BrokerName.FUBON_QFF),
        binance_adapter=FakeLiveExecutionAdapter(BrokerName.BINANCE_TSM),
        readonly_brokers=readonly,
        clock=dry_run_clock(
            [
                "2026-06-18T08:47:02+08:00",
                "2026-06-18T08:47:30+08:00",
                "2026-06-18T08:47:59+08:00",
                "2026-06-18T08:48:01+08:00",
                "2026-06-18T08:48:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal, color=False),
    ).run(resume=True, max_iterations=3)
    return terminal.getvalue()


def _live_execute_resume_config(tmp_path, monkeypatch):
    config = small_live_config(tmp_path)
    config = replace(
        config,
        safety=replace(config.safety, allow_live_order=True),
        strategy=replace(config.strategy, entry_z=1.0),
        live_execution=replace(config.live_execution, enabled=True),
    )
    for name in (
        "PROJECT_LUX_ALLOW_LIVE_ORDER",
        "FUBON_ALLOW_LIVE_ORDER",
        "BINANCE_ALLOW_LIVE_ORDER",
    ):
        monkeypatch.setenv(name, "1")
    return config


def test_live_execute_resume_keeps_open_when_broker_matches(
    tmp_path, monkeypatch
) -> None:
    config = _live_execute_resume_config(tmp_path, monkeypatch)
    _run_live_execute_entry_to_open(config)

    output = _resume_live_execute(
        config,
        readonly=_live_execute_resume_brokers(
            qff_quantity=100.0,
            tsm_quantity=-(1_000_000.0 / 120.0),
            at="2026-06-18T08:47:30+08:00",
        ),
    )

    assert "resume_reconciliation matched" in output
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        state = store.load_resume_state()
        assert state is not None
        # A matching broker must NOT trigger a false pause on restart.
        assert state.strategy.state == StrategyState.OPEN
    finally:
        store.close()


def test_live_execute_resume_pauses_when_broker_lost_position(
    tmp_path, monkeypatch
) -> None:
    config = _live_execute_resume_config(tmp_path, monkeypatch)
    _run_live_execute_entry_to_open(config)

    # Broker now reports a flat position (liquidated / closed during downtime).
    output = _resume_live_execute(
        config,
        readonly=_live_execute_resume_brokers(
            qff_quantity=0.0,
            tsm_quantity=0.0,
            at="2026-06-18T08:47:30+08:00",
        ),
    )

    assert "resume_reconciliation" in output
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.PAUSED
    finally:
        store.close()


class _RaisingReadOnlyBroker:
    def __init__(self, broker: BrokerName) -> None:
        self.broker = broker

    def fetch_snapshot(self):
        raise RuntimeError(f"{self.broker.value} read-only API unavailable")

    def close(self) -> None:
        return None


def test_live_execute_resume_pauses_when_broker_unreachable(
    tmp_path, monkeypatch
) -> None:
    config = _live_execute_resume_config(tmp_path, monkeypatch)
    _run_live_execute_entry_to_open(config)

    # Read-only API is unreachable at restart: reconciliation cannot confirm the
    # restored position, so resume must pause rather than crash or trade blind.
    _resume_live_execute(
        config,
        readonly=(
            _RaisingReadOnlyBroker(BrokerName.FUBON_QFF),
            FixedPositionReadOnlyBroker(
                broker=BrokerName.BINANCE_TSM,
                symbol="TSM/USDT:USDT",
                quantity=-(1_000_000.0 / 120.0),
                fetched_at=ts("2026-06-18T08:47:30+08:00"),
            ),
        ),
    )

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.PAUSED
    finally:
        store.close()


def test_live_dry_run_resume_does_not_duplicate_recorded_intent(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, strategy=replace(config.strategy, entry_z=1.0))
    first_qff, first_tsm, first_usd = dry_run_quote_providers(
        [
            "2026-06-18T08:45:30+08:00",
            "2026-06-18T08:45:59+08:00",
            "2026-06-18T08:46:01+08:00",
            "2026-06-18T08:46:59+08:00",
            "2026-06-18T08:47:01+08:00",
        ]
    )

    first_result = LiveDryRunRunner(
        config,
        qff_provider=first_qff,
        tsm_provider=first_tsm,
        usdttwd_provider=first_usd,
        clock=dry_run_clock(
            [
                "2026-06-18T08:45:00+08:00",
                "2026-06-18T08:45:30+08:00",
                "2026-06-18T08:45:59+08:00",
                "2026-06-18T08:46:01+08:00",
                "2026-06-18T08:46:59+08:00",
                "2026-06-18T08:47:01+08:00",
                "2026-06-18T08:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(reset_store=True, max_iterations=5)

    assert first_result.plans_recorded == 1

    second_qff, second_tsm, second_usd = dry_run_quote_providers(
        [
            "2026-06-18T08:47:30+08:00",
            "2026-06-18T08:47:59+08:00",
            "2026-06-18T08:48:01+08:00",
        ],
        qff_rows=pd.DataFrame(),
        tsm_rows=pd.DataFrame(),
        usd_rows=pd.DataFrame(),
    )
    second_result = LiveDryRunRunner(
        config,
        qff_provider=second_qff,
        tsm_provider=second_tsm,
        usdttwd_provider=second_usd,
        clock=dry_run_clock(
            [
                "2026-06-18T08:47:02+08:00",
                "2026-06-18T08:47:30+08:00",
                "2026-06-18T08:47:59+08:00",
                "2026-06-18T08:48:01+08:00",
                "2026-06-18T08:48:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(resume=True, max_iterations=3)

    assert second_result.plans_recorded == 0

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "warmup_bars") == config.live.warmup_minutes
        assert count_table(store, "live_runs") == 2
        assert count_table(store, "execution_plans") == 1
        assert count_table(store, "execution_outcomes") == 1
        duplicate_bars = store.connection.execute(
            """
            SELECT COUNT(*)
            FROM (
                SELECT timestamp
                FROM bars
                GROUP BY timestamp
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert duplicate_bars == 0
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.OPEN
    finally:
        store.close()


def test_reconnect_qff_provider_if_supported_relogins_and_stays_safe() -> None:
    from lux_trader.runtime.live.contracts import reconnect_qff_provider_if_supported

    class _ReconProvider:
        def __init__(self, exc: Exception | None = None) -> None:
            self.calls = 0
            self.exc = exc

        def reconnect(self) -> None:
            self.calls += 1
            if self.exc is not None:
                raise self.exc

    when = ts("2026-06-18T08:45:00+08:00")

    out = io.StringIO()
    provider = _ReconProvider()
    reconnect_qff_provider_if_supported(provider, LiveTerminalReporter(out, color=False), when)
    assert provider.calls == 1
    assert "reconnect_login" in out.getvalue()

    out_fail = io.StringIO()
    raising = _ReconProvider(exc=RuntimeError("login boom"))
    reconnect_qff_provider_if_supported(
        raising, LiveTerminalReporter(out_fail, color=False), when
    )
    assert raising.calls == 1  # attempted
    assert "reconnect_failed" in out_fail.getvalue()  # caught, not propagated

    # A provider without reconnect support must be a no-op, never an error.
    reconnect_qff_provider_if_supported(object(), LiveTerminalReporter(io.StringIO(), color=False), when)


def test_live_dry_run_survives_contract_resolution_failure(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, strategy=replace(config.strategy, entry_z=1.0))
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T08:45:30+08:00",
            "2026-06-18T08:45:59+08:00",
            "2026-06-18T08:46:01+08:00",
            "2026-06-18T08:46:59+08:00",
            "2026-06-18T08:47:01+08:00",
        ]
    )
    # Startup resolves the contract once successfully; every later per-bar
    # re-resolution raises (mimicking a Fugle token-expired ticker lookup).
    calls = {"n": 0}
    real_select = qff.select_front_month_symbol

    def failing_select(product: str) -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("Fubon QFF ticker lookup token expired")
        return real_select(product)

    qff.select_front_month_symbol = failing_select
    terminal = io.StringIO()

    # The loop must survive the resolution failures rather than crash.
    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T08:45:00+08:00",
                "2026-06-18T08:45:30+08:00",
                "2026-06-18T08:45:59+08:00",
                "2026-06-18T08:46:01+08:00",
                "2026-06-18T08:46:59+08:00",
                "2026-06-18T08:47:01+08:00",
                "2026-06-18T08:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal, color=False),
    ).run(reset_store=True, max_iterations=5)

    assert calls["n"] > 1  # per-bar resolution was actually attempted and failed
    assert result.bars_processed >= 1  # bars still processed on the current contract
    assert "resolve_failed" in terminal.getvalue()


def seed_strategy_state(config: AppConfig, state: StrategyRuntimeState) -> None:
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        store.save_state(
            0,
            state.exit_signal_time
            or state.candidate_time
            or ts("2026-06-18T08:45:00+08:00"),
            state,
            IndicatorEngine(window=config.strategy.zscore_window),
        )
        store.commit()
    finally:
        store.close()


def open_position_state(*, state: StrategyState) -> StrategyRuntimeState:
    entry_time = ts("2026-06-18T08:40:00+08:00")
    return StrategyRuntimeState(
        state=state,
        position_direction=Direction.SHORT_TSM_LONG_QFF,
        exit_signal_idx=0 if state == StrategyState.EXIT_PENDING else -1,
        exit_signal_time=ts("2026-06-18T08:45:00+08:00")
        if state == StrategyState.EXIT_PENDING
        else None,
        exit_signal_zscore=-0.1 if state == StrategyState.EXIT_PENDING else None,
        entry_tsm=100.0,
        entry_qff=100.0,
        entry_zscore=2.2,
        tsm_units=-10_000.0,
        qff_units=10_000.0,
        qff_contracts=100,
        actual_leg_notional_twd=1_000_000.0,
        running_max_equity=2_000_000.0,
        open_trade={
            "entry_signal_idx": 0,
            "entry_signal_time": entry_time,
            "entry_signal_zscore": 2.2,
            "entry_idx": 0,
            "entry_time": entry_time,
            "entry_delay_minutes": 1,
            "entry_fill_zscore": 2.1,
            "direction": Direction.SHORT_TSM_LONG_QFF.value,
            "entry_tsm_twd_fair": 100.0,
            "entry_qff_close": 100.0,
            "tsm_units": -10_000.0,
            "qff_units": 10_000.0,
            "qff_contracts": 100,
            "raw_qff_contracts": 100.0,
            "leg_notional_twd": 1_000_000.0,
            "actual_leg_notional_twd": 1_000_000.0,
            "qff_contract_multiplier": 100.0,
            "entry_tsm_fee_twd": 500.0,
            "entry_qff_fee_twd": 500.0,
            "entry_qff_tax_twd": 2.0,
            "entry_fee_twd": 1002.0,
            "qff_symbol": "QFF202607",
            "qff_expiry": "2026-07-15",
            "contract_policy_state": "active",
        },
        trading_qff_symbol="QFF202607",
        trading_qff_expiry="2026-07-15",
        eligible_active_qff_symbol="QFF202607",
        eligible_active_qff_expiry="2026-07-15",
        last_warmup_symbol="QFF202607",
        contract_policy_state="active",
    )


def test_live_dry_run_exit_pending_records_exit_intent(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_strategy_state(config, open_position_state(state=StrategyState.EXIT_PENDING))
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T08:45:30+08:00",
            "2026-06-18T08:45:59+08:00",
            "2026-06-18T08:46:01+08:00",
        ]
    )

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T08:45:00+08:00",
                "2026-06-18T08:45:30+08:00",
                "2026-06-18T08:45:59+08:00",
                "2026-06-18T08:46:01+08:00",
                "2026-06-18T08:46:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(resume=True, max_iterations=3)

    assert result.plans_recorded == 1
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        plan = store.load_latest_execution_plan_payload()
        assert plan is not None
        assert plan["plan_type"] == "exit"
        assert plan["status"] == "recorded"
        assert plan["reason"] == "dry_run_exit_intent"
        sides = {leg["broker"]: leg["side"] for leg in plan["legs"]}
        assert sides[BrokerName.BINANCE_TSM.value] == OrderSide.BUY.value
        assert sides[BrokerName.FUBON_QFF.value] == OrderSide.SELL.value
        assert plan["qff_symbol"] == "QFF202607"
        assert plan["qff_expiry"] == "2026-07-15"
        assert plan["contract_policy_state"] == "active"
        assert count_table(store, "execution_outcomes") == 1
        assert count_table(store, "orders") == 2
        assert count_table(store, "fills") == 2
        assert count_table(store, "trades") == 1
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.FLAT
    finally:
        store.close()


def test_live_dry_run_force_exit_records_rollover_exit_intent(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = open_position_state(state=StrategyState.OPEN)
    state.trading_qff_expiry = "2026-06-19"
    state.open_trade["qff_expiry"] = "2026-06-19"
    seed_strategy_state(config, state)
    force_qff_rows = rows(
        [
            ("2026-06-18T13:32:00+08:00", 100.0),
            ("2026-06-18T13:33:00+08:00", 100.0),
            ("2026-06-18T13:34:00+08:00", 100.0),
        ]
    )
    force_tsm_rows = rows(
        [
            ("2026-06-18T13:32:00+08:00", 20.0),
            ("2026-06-18T13:33:00+08:00", 20.0),
            ("2026-06-18T13:34:00+08:00", 20.0),
        ]
    )
    force_usd_rows = rows(
        [
            ("2026-06-18T13:32:00+08:00", 25.0),
            ("2026-06-18T13:33:00+08:00", 25.0),
            ("2026-06-18T13:34:00+08:00", 25.0),
        ]
    )
    qff, tsm, usd = dry_run_quote_providers(
        [
            "2026-06-18T13:35:30+08:00",
            "2026-06-18T13:35:59+08:00",
            "2026-06-18T13:36:01+08:00",
        ],
        qff_rows=force_qff_rows,
        tsm_rows=force_tsm_rows,
        usd_rows=force_usd_rows,
    )

    result = LiveDryRunRunner(
        config,
        qff_provider=qff,
        tsm_provider=tsm,
        usdttwd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T13:35:00+08:00",
                "2026-06-18T13:35:30+08:00",
                "2026-06-18T13:35:59+08:00",
                "2026-06-18T13:36:01+08:00",
                "2026-06-18T13:36:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(resume=True, max_iterations=3)

    assert result.plans_recorded == 1
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        plan = store.load_latest_execution_plan_payload()
        assert plan is not None
        assert plan["plan_type"] == "exit"
        assert plan["reason"] == "rollover_force_exit"
        event_types = [
            row["event_type"]
            for row in store.connection.execute(
                "SELECT event_type FROM events ORDER BY event_id"
            ).fetchall()
        ]
        assert "rollover_force_exit" in event_types
        assert count_table(store, "execution_outcomes") == 1
        assert count_table(store, "orders") == 2
        assert count_table(store, "fills") == 2
        assert count_table(store, "trades") == 1
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.FLAT
    finally:
        store.close()


def test_live_paper_auto_warmup_builds_seed_on_empty_store(tmp_path) -> None:
    config = small_live_config(tmp_path)
    qff = FakeQffProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 100.0),
                ("2026-06-18T04:59:00+08:00", 101.0),
                ("2026-06-18T05:00:00+08:00", 102.0),
            ]
        )
    )
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 20.0),
                ("2026-06-18T04:59:00+08:00", 20.5),
                ("2026-06-18T05:00:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 30.0),
                ("2026-06-18T04:59:00+08:00", 30.0),
                ("2026-06-18T05:00:00+08:00", 30.0),
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
                ("2026-06-18T04:58:00+08:00", 100.0),
                ("2026-06-18T04:59:00+08:00", 101.0),
                ("2026-06-18T05:00:00+08:00", 102.0),
            ]
        )
    )
    warmup_tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 20.0),
                ("2026-06-18T04:59:00+08:00", 20.5),
                ("2026-06-18T05:00:00+08:00", 21.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 30.0),
                ("2026-06-18T04:59:00+08:00", 30.0),
                ("2026-06-18T05:00:00+08:00", 30.0),
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
                ("2026-06-18T04:58:00+08:00", 100.0),
                ("2026-06-18T04:59:00+08:00", 101.0),
                ("2026-06-18T05:00:00+08:00", 102.0),
            ]
        )
    )
    tsm = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 20.0),
                ("2026-06-18T04:59:00+08:00", 20.5),
                ("2026-06-18T05:00:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T04:58:00+08:00", 30.0),
                ("2026-06-18T04:59:00+08:00", 30.0),
                ("2026-06-18T05:00:00+08:00", 30.0),
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

    decision, decision_type, decision_zscore, signal_block_reason = build_live_decision_snapshot(
        config,
        state,
        snapshot,
        tradable,
    )

    assert not decision.zscore_valid
    assert decision.zscore is None
    assert decision_type is None
    assert decision_zscore is None
    assert signal_block_reason is None


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

    decision, decision_type, decision_zscore, signal_block_reason = build_live_decision_snapshot(
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
    assert signal_block_reason is None


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

    decision, decision_type, decision_zscore, signal_block_reason = build_live_decision_snapshot(
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
    assert signal_block_reason is None


def test_live_decision_reports_tradable_snapshot_missing_reason(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(state=StrategyState.FLAT)
    tradable = TradableSpreadSnapshot(
        mid_spread=1.7,
        mid_zscore=1.7,
        short_spread=None,
        short_zscore=None,
        long_spread=None,
        long_zscore=None,
        missing_reason="stale_qff",
    )

    decision, decision_type, decision_zscore, signal_block_reason = (
        build_live_decision_snapshot(
            config,
            state,
            indicator_snapshot(zscore=1.7),
            tradable,
        )
    )

    assert not decision.zscore_valid
    assert decision_type is None
    assert decision_zscore is None
    assert signal_block_reason == "stale_qff"


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
