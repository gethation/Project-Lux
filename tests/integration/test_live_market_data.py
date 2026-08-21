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
from lux_trader.integrations.fubon.market_data import (
    FubonCcfMarketData,
    parse_fubon_books_quote,
)
from lux_trader.integrations.taifex.downloader import (
    TaifexCcfTradeDownloader,
    parse_taifex_download_entries,
)
from lux_trader.market_data import (
    LiveMinuteBarBuilder,
    LiveQuote,
    LiveQuoteSet,
    WarmupBuilder,
    build_ccf_expected_warmup_index,
    build_ccf_warmup_source_report,
    parse_timestamp,
    ccf_symbol_to_taifex_contract_month,
    select_ccf_front_month,
)
from lux_trader.runtime.live import LiveDryRunRunner, LiveExecuteRunner
from lux_trader.runtime.live.modes import (
    DryRunLiveModeHandler,
    LiveExecuteModeHandler,
)
from lux_trader.runtime.live.bootstrap import (
    WindowsTimeSyncResult,
    run_live_startup_preflight,
)
from lux_trader.runtime.live.contracts import (
    CcfContractResolution,
    cancel_entry_pending_for_contract_switch,
    mark_pending_contract_switch_if_needed,
    resolve_ccf_contract,
    resolve_force_exit_reason,
    should_force_exit_for_contract_policy,
    should_switch_contract_before_processing,
    switch_to_contract,
)
from lux_trader.runtime.live.engine import build_live_decision_snapshot
from lux_trader.runtime.live.warmup import (
    CcfWarmupCheckRunner,
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
from lux_trader.terminal_ui import LiveTerminalReporter, NullLiveReporter
from lux_trader.core.tradable_spread import TradableSpreadSnapshot

from conftest import make_app_config


class FakeCcfProvider:
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
        return "CCF202607"

    def fetch_1m(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.fetch_1m_calls.append((symbol, start, end))
        return self.rows.copy()

    def fetch_quote(self, symbol: str) -> LiveQuote:
        self.quote_calls.append(symbol)
        if not self.quotes:
            raise RuntimeError("No fake CCF quotes left")
        quote = self.quotes.pop(0)
        if isinstance(quote, Exception):
            raise quote
        return quote

    def teardown_books_session(self) -> None:
        self.teardown_books_calls += 1

    def restart_books_session(self, symbol: str, *, after_hours: bool | None = None) -> None:
        self.restart_books_calls.append(symbol)


class FakeCcfCandidateProvider(FakeCcfProvider):
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        super().__init__()
        self.candidates = candidates

    def fetch_candidates(self, product: str) -> list[dict[str, object]]:
        return self.candidates


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
    def __init__(self, broker: BrokerName, *, position_quantity: float = 0.0) -> None:
        self.broker = broker
        self.plans: list[PairExecutionPlan] = []
        # The coordinator reads this before sending anything; an entry plan
        # expects flat. See execution/position_guard.py.
        self.position_quantity = float(position_quantity)

    def fetch_position_quantity(self) -> float:
        return self.position_quantity

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
            ccf_symbol=leg.ccf_symbol,
            ccf_expiry=leg.ccf_expiry,
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
        self.candle_calls: list[dict[str, object]] = []

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
                    "lastUpdated": "2026-06-18T02:45:01+08:00",
                }
            },
        )

    def candles(self, **kwargs: object) -> object:
        self.candle_calls.append(kwargs)
        key = (
            "candles_afterhours"
            if kwargs.get("session") == "afterhours"
            else "candles_regular"
        )
        response = self.responses.get(key, {"data": []})
        if isinstance(response, Exception):
            raise response
        return response


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
            ccf_book_stale_seconds=55.0,
            sync_windows_time_on_startup=True,
            clock_skew_fail_seconds=60.0,
            windows_time_sync_timeout_seconds=15.0,
            max_leg_timestamp_skew_seconds=10.0,
            warmup_minutes=3,
            ccf_product="CCF",
            ccf_symbol="auto",
            umc_symbol="UMC",
            fx_symbol="USD/TWD",
            fubon_env_path=None,
            taifex_ccf_1m_csv=None,
            taifex_use_network=False,
            taifex_cache_dir=tmp_path / "taifex_cache",
        ),
    )


def write_minimal_config(tmp_path: Path, live_body: str = "") -> Path:
    config_path = tmp_path / "config.test.toml"
    config_path.write_text(
        "\n".join(
            [
                "[fees]",
                "ccf_contract_multiplier = 2000.0",
                "",
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
    # Both CCF budgets were 55.0, a number inherited from a doctor warning and
    # never measured against the feed. Measured 2026-08-15 across six sessions:
    # book publishes reach 478s, and a legitimate forward-fill carry reaches
    # 1080s. 55 rejected 35.8% of all minutes, because the carry is always a
    # multiple of 60 and 55 sits below the first one.
    assert config.live.ccf_book_stale_seconds == pytest.approx(600.0)
    assert config.live.ccf_forward_fill_max_seconds == pytest.approx(900.0)
    assert config.live.sync_windows_time_on_startup is True
    assert config.live.clock_skew_fail_seconds == pytest.approx(60.0)
    assert config.live.windows_time_sync_timeout_seconds == pytest.approx(15.0)
    assert config.live_execution_smoke.enabled is False
    assert config.live_execution_smoke.fubon_lots == 1
    assert config.live_execution_smoke.umc_units == pytest.approx(0.1)


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
                    "ccf_book_stale_seconds = 42.5",
                    "sync_windows_time_on_startup = false",
                    "clock_skew_fail_seconds = 12.5",
                    "windows_time_sync_timeout_seconds = 3.0",
                ]
            ),
        )
    )

    assert config.live.stale_seconds == pytest.approx(10.0)
    assert config.live.ccf_book_stale_seconds == pytest.approx(42.5)
    assert config.live.sync_windows_time_on_startup is False
    assert config.live.clock_skew_fail_seconds == pytest.approx(12.5)
    assert config.live.windows_time_sync_timeout_seconds == pytest.approx(3.0)


def test_load_config_reads_live_execution_smoke_config(tmp_path) -> None:
    config_path = tmp_path / "config.test.toml"
    config_path.write_text(
        "\n".join(
            [
                "[fees]",
                "ccf_contract_multiplier = 2000.0",
                "",
                "[paths]",
                "store_path = 'project_lux.sqlite3'",
                "input_csv = ''",
                "",
                "[live_execution_smoke]",
                "enabled = true",
                "fubon_symbol = 'TMFG6'",
                "fubon_lots = 1",
                "umc_symbol = 'UMC'",
                "umc_units = 0.1",
                "ccf_expiry = '202607'",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.live_execution_smoke.enabled is True
    assert config.live_execution_smoke.fubon_symbol == "TMFG6"
    assert config.live_execution_smoke.fubon_lots == 1
    assert config.live_execution_smoke.umc_symbol == "UMC"
    assert config.live_execution_smoke.umc_units == pytest.approx(0.1)
    assert config.live_execution_smoke.ccf_expiry == "202607"


def test_live_startup_preflight_syncs_windows_time_and_accepts_clock_skew(tmp_path) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()
    sync_timeouts: list[float] = []
    probe_calls: list[tuple] = []

    def sync_runner(timeout_seconds: float) -> WindowsTimeSyncResult:
        sync_timeouts.append(timeout_seconds)
        return WindowsTimeSyncResult(True, "ok")

    def reference_time_probe(servers, *, timeout_seconds) -> datetime:
        probe_calls.append((tuple(servers), timeout_seconds))
        return ts("2026-06-23T11:45:00+08:00")

    run_live_startup_preflight(
        config,
        LiveTerminalReporter(terminal_output, color=False),
        lambda: ts("2026-06-23T11:45:00+08:00"),
        platform_name="win32",
        sync_runner=sync_runner,
        reference_time_probe=reference_time_probe,
    )

    assert sync_timeouts == [15.0]
    # The configured NTP servers, not a venue symbol: the gate checks absolute
    # time, which is what a drifting local clock actually breaks.
    assert probe_calls == [(config.live.ntp_servers, config.live.ntp_timeout_seconds)]
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
        reference_time_probe=lambda *_a, **_k: ts("2026-06-23T11:44:59+08:00"),
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
        reference_time_probe=lambda *_a, **_k: ts("2026-06-23T11:45:00+08:00"),
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
            reference_time_probe=lambda *_a, **_k: ts("2026-06-23T11:45:00+08:00"),
        )

    output = terminal_output.getvalue()
    assert "EVENT startup sync_windows_time" in output
    assert "ERR clock_skew local=2026-06-23T03:45:00+08:00" in output


def test_live_startup_preflight_rejects_unavailable_market_time(tmp_path) -> None:
    config = small_live_config(tmp_path)
    terminal_output = io.StringIO()

    def reference_time_probe(*_a, **_k) -> datetime:
        raise RuntimeError("no NTP route")

    with pytest.raises(RuntimeError, match="Unable to verify clock skew against NTP"):
        run_live_startup_preflight(
            config,
            LiveTerminalReporter(terminal_output, color=False),
            lambda: ts("2026-06-23T11:45:00+08:00"),
            platform_name="win32",
            sync_runner=lambda _: WindowsTimeSyncResult(True, "ok"),
            reference_time_probe=reference_time_probe,
        )

    assert "ERR clock_skew unavailable:RuntimeError" in terminal_output.getvalue()


def test_live_runtime_clock_preflight_failure_stops_before_ccf_provider(
    tmp_path,
    monkeypatch,
) -> None:
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(pd.DataFrame())
    umc = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())

    def fail_preflight(*_: object, **__: object) -> None:
        raise RuntimeError("clock skew test")

    monkeypatch.setattr(live_engine, "run_live_startup_preflight", fail_preflight)

    with pytest.raises(RuntimeError, match="clock skew test"):
        LiveDryRunRunner(
            config,
            ccf_provider=ccf,
            umc_provider=umc,
            usd_twd_provider=usd,
            sleeper=lambda _: None,
        ).run(max_iterations=0)

    assert ccf.select_calls == 0
    assert ccf.fetch_1m_calls == []
    assert ccf.quote_calls == []
    assert ccf.restart_books_calls == []


def test_live_runtime_skips_clock_preflight_when_clock_is_injected(
    tmp_path,
    monkeypatch,
) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    ccf = FakeCcfProvider(pd.DataFrame())
    umc = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())

    def fail_preflight(*_: object, **__: object) -> None:
        raise AssertionError("preflight should be skipped")

    monkeypatch.setattr(live_engine, "run_live_startup_preflight", fail_preflight)

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=lambda: ts("2026-06-23T02:45:00+08:00"),
        sleeper=lambda _: None,
    ).run(max_iterations=0, skip_warmup=True)

    assert result.iterations == 0
    assert ccf.select_calls == 1


def count_table(store: SQLiteStore, table: str) -> int:
    row = store.connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def dry_run_warmup_rows() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    return (
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 100.0),
                ("2026-06-18T02:44:00+08:00", 100.0),
            ]
        ),
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.0),
                ("2026-06-18T02:44:00+08:00", 20.0),
            ]
        ),
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 25.0),
                ("2026-06-18T02:43:00+08:00", 25.0),
                ("2026-06-18T02:44:00+08:00", 25.0),
            ]
        ),
    )


def seed_warmup_bars(config: AppConfig) -> None:
    bars = [
        MarketBar(
            row_index=index,
            timestamp=ts(f"2026-06-18T02:4{index}:00+08:00"),
            ccf_close=100.0,
            ccf_close_filled=100.0,
            umc_twd_fair=100.0 + index,
            spread=float(index),
            ccf_symbol="CCFG6",
            ccf_expiry="2026-07-15",
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
    ccf_rows: pd.DataFrame | None = None,
    umc_rows: pd.DataFrame | None = None,
    usd_rows: pd.DataFrame | None = None,
    ccf_price: float = 100.0,
    umc_price: float = 20.0,
    usd_price: float = 30.0,
) -> tuple[FakeCcfProvider, FakeOhlcvProvider, FakeOhlcvProvider]:
    default_ccf_rows, default_umc_rows, default_usd_rows = dry_run_warmup_rows()
    return (
        FakeCcfProvider(
            ccf_rows if ccf_rows is not None else default_ccf_rows,
            quotes=[
                quote("ccf", value, ccf_price, bid=ccf_price - 0.1, ask=ccf_price + 0.1)
                for value in quote_times
            ],
        ),
        FakeOhlcvProvider(
            umc_rows if umc_rows is not None else default_umc_rows,
            quotes=[
                quote("umc", value, umc_price, bid=umc_price - 0.01, ask=umc_price + 0.01)
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


def test_live_dry_run_closed_calendar_skips_market_data_bars_and_margin_checks(
    tmp_path,
    monkeypatch,
) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        trading_calendar=replace(
            config.trading_calendar,
            closed_dates=(date(2026, 6, 19),),
        ),
    )
    seed_warmup_bars(config)
    ccf = FakeCcfProvider(pd.DataFrame())
    umc = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())
    terminal_output = io.StringIO()
    margin_check_calls: list[datetime] = []

    class RecordingMarginMonitor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def maybe_run(self, observed_at, **kwargs) -> None:
            margin_check_calls.append(observed_at)

        def close(self) -> None:
            pass

    monkeypatch.setattr(live_engine, "MarginMonitor", RecordingMarginMonitor)

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
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
    assert ccf.quote_calls == []
    assert umc.quote_calls == []
    assert usd.quote_calls == []
    assert margin_check_calls == []
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


def test_live_runtime_tears_down_ccf_books_during_non_trading_and_restarts_on_open(
    tmp_path,
) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    # In session -> out -> in again, on the PAIR's clock. On Tuesday 06-23 that
    # is 03:58 (tail of Monday's US session), 04:01 (RTH has closed; TAIFEX is
    # still open but the pair is not), then 21:30 (Tuesday's US open).
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-23T03:58:01+08:00",
            "2026-06-23T21:30:00+08:00",
        ]
    )
    trading_session_events: list[datetime] = []

    class RecordingSessionReporter(NullLiveReporter):
        def trading_session(self, timestamp) -> None:
            trading_session_events.append(timestamp)

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T03:58:00+08:00",
                "2026-06-23T03:58:01+08:00",
                "2026-06-23T04:01:00+08:00",
                "2026-06-23T21:30:00+08:00",
                "2026-06-23T21:30:01+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=RecordingSessionReporter(),
    ).run(max_iterations=3, skip_warmup=True)

    assert result.iterations == 3
    assert ccf.teardown_books_calls == 1
    assert ccf.restart_books_calls == ["CCFG6"]
    assert ccf.quote_calls == ["CCFG6", "CCFG6"]
    assert trading_session_events == [ts("2026-06-23T21:30:00+08:00")]


def test_live_runtime_ccf_watchdog_restarts_once_with_backoff(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    ccf_rows, umc_rows, usd_rows = dry_run_warmup_rows()
    ccf = FakeCcfProvider(
        ccf_rows,
        quotes=[
            quote("ccf", "2026-06-23T02:40:00+08:00", 100.0, bid=99.9, ask=100.1),
            quote("ccf", "2026-06-23T02:40:00+08:00", 100.0, bid=99.9, ask=100.1),
            quote("ccf", "2026-06-23T02:40:00+08:00", 100.0, bid=99.9, ask=100.1),
        ],
    )
    umc = FakeOhlcvProvider(
        umc_rows,
        quotes=[
            quote("umc", "2026-06-23T02:45:01+08:00", 20.0, bid=19.99, ask=20.01),
            quote("umc", "2026-06-23T02:45:11+08:00", 20.0, bid=19.99, ask=20.01),
            quote("umc", "2026-06-23T02:45:20+08:00", 20.0, bid=19.99, ask=20.01),
        ],
    )
    usd = FakeOhlcvProvider(
        usd_rows,
        quotes=[
            quote("usd", "2026-06-23T02:45:01+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-23T02:45:11+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-23T02:45:20+08:00", 30.0, bid=29.99, ask=30.01),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T02:45:00+08:00",
                "2026-06-23T02:45:01+08:00",
                "2026-06-23T02:45:11+08:00",
                "2026-06-23T02:45:20+08:00",
                "2026-06-23T02:45:21+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=3, skip_warmup=True)

    assert result.iterations == 3
    assert ccf.restart_books_calls == ["CCFG6"]
    assert "WARN ccf_reconnecting skip_signal" in terminal_output.getvalue()


def test_live_runtime_uses_cached_quote_after_transient_fetch_failure(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    ccf, _, usd = dry_run_quote_providers(
        [
            "2026-06-23T02:45:00+08:00",
            "2026-06-23T02:45:01+08:00",
        ]
    )
    umc = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("umc", "2026-06-23T02:45:00+08:00", 20.0, bid=19.99, ask=20.01),
            RuntimeError("request timeout"),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T02:45:00+08:00",
                "2026-06-23T02:45:00+08:00",
                "2026-06-23T02:45:01+08:00",
                "2026-06-23T02:45:01+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=2, skip_warmup=True)

    assert result.iterations == 2
    assert "WARN fetch_umc failed:RuntimeError" in terminal_output.getvalue()
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "market_ticks") == 6
    finally:
        store.close()


def test_live_runtime_skips_iteration_when_fetch_fails_without_cached_quote(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    ccf = FakeCcfProvider(
        pd.DataFrame(),
        quotes=[
            quote("ccf", "2026-06-23T02:45:00+08:00", 100.0, bid=99.9, ask=100.1),
        ],
    )
    umc = FakeOhlcvProvider(pd.DataFrame(), quotes=[RuntimeError("request timeout")])
    usd = FakeOhlcvProvider(
        pd.DataFrame(),
        quotes=[
            quote("usd", "2026-06-23T02:45:00+08:00", 30.0, bid=29.99, ask=30.01),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=lambda: ts("2026-06-23T02:45:00+08:00"),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(max_iterations=1, skip_warmup=True)

    assert result.iterations == 1
    output = terminal_output.getvalue()
    assert "WARN fetch_umc failed:RuntimeError" in output
    assert "WARN market_data_fetch skip_iteration" in output
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        assert count_table(store, "market_ticks") == 0
    finally:
        store.close()


def indicator_snapshot(zscore: float = 2.1) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        timestamp=ts("2026-06-18T02:45:00+08:00"),
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
    provider = FubonCcfMarketData(None)
    intraday = FakeFubonIntraday(
        {
            "REGULAR": {"data": []},
            "AFTERHOURS": {
                "data": [
                    {
                        "symbol": "CCFH6",
                        "product": "CCF",
                        "endDate": "2026-08-19",
                    }
                ]
            },
        }
    )
    provider.intraday = intraday

    candidates = provider.fetch_candidates("CCF")

    assert intraday.sessions == ["REGULAR", "AFTERHOURS"]
    assert len(candidates) == 1
    assert candidates[0]["symbol"] == "CCFH6"
    assert provider.last_candidate_session_counts == {
        "REGULAR": 0,
        "AFTERHOURS": 1,
    }
    assert "sample=" in provider.last_candidate_session_summaries["AFTERHOURS"]


def test_fubon_fetch_candidates_empty_sessions_error_includes_diagnostics() -> None:
    provider = FubonCcfMarketData(None)
    intraday = FakeFubonIntraday(
        {
            "REGULAR": {"data": []},
            "AFTERHOURS": {"data": []},
        }
    )
    provider.intraday = intraday

    with pytest.raises(RuntimeError) as error:
        provider.fetch_candidates("CCF")

    message = str(error.value)
    assert intraday.sessions == ["REGULAR", "AFTERHOURS"]
    assert "session_counts={'REGULAR': 0, 'AFTERHOURS': 0}" in message
    assert "session_summaries=" in message
    assert provider.last_candidate_session_counts == {
        "REGULAR": 0,
        "AFTERHOURS": 0,
    }


def test_select_ccf_front_month_skips_expired_contracts() -> None:
    selected = select_ccf_front_month(
        [
            {"symbol": "CCF202606", "contractMonth": "202606"},
            {"symbol": "CCF202608", "contractMonth": "202608"},
            {"symbol": "CCF202607", "contractMonth": "202607"},
        ],
        product="CCF",
        today=datetime.fromisoformat("2026-06-18T00:00:00+08:00").date(),
    )

    assert selected.symbol == "CCF202607"


def test_select_ccf_front_month_accepts_fubon_end_date_fields() -> None:
    selected = select_ccf_front_month(
        [
            {"symbol": "CCFG6", "endDate": "2026-07-15"},
            {"symbol": "CCFH6", "settlementDate": "2026-08-19"},
            {"symbol": "CCFC7", "endDate": "2027-03-17"},
        ],
        product="CCF",
        today=datetime.fromisoformat("2026-06-18T00:00:00+08:00").date(),
    )

    assert selected.symbol == "CCFG6"


def test_select_ccf_front_month_fails_when_expiry_is_unparseable() -> None:
    with pytest.raises(RuntimeError, match="Unable to select"):
        select_ccf_front_month([{"symbol": "CCFUNKNOWN"}], product="CCF")


def test_ccf_symbol_to_taifex_contract_month_accepts_fubon_code() -> None:
    assert (
        ccf_symbol_to_taifex_contract_month(
            "CCFG6",
            reference_date=datetime.fromisoformat("2026-06-18T00:00:00+08:00").date(),
        )
        == "202607"
    )


def test_resolve_ccf_contract_normalizes_policy_selection_for_fubon_ordering(tmp_path) -> None:
    config = make_app_config(tmp_path)
    contract = resolve_ccf_contract(
        config,
        FakeCcfCandidateProvider(
            [{"symbol": "CCF202607", "contractMonth": "202607"}]
        ),
        now=ts("2026-06-18T02:45:00+08:00"),
    )

    assert contract.symbol == "CCFG6"
    assert contract.expiry == "2026-07-15"
    assert contract.selection is not None
    assert contract.selection.symbol == "CCF202607"


def test_resolve_ccf_contract_normalizes_front_month_selector_for_fubon_ordering(tmp_path) -> None:
    config = make_app_config(tmp_path)
    config = replace(
        config,
        contract_policy=replace(config.contract_policy, enabled=False),
    )

    contract = resolve_ccf_contract(
        config,
        FakeCcfProvider(),
        now=ts("2026-06-18T02:45:00+08:00"),
    )

    assert contract.symbol == "CCFG6"
    assert contract.expiry is None
    assert contract.policy_state == "front_month"


def test_parse_timestamp_accepts_fubon_microsecond_epoch() -> None:
    # Epoch arithmetic, not a session time: this one stays at 13:30 Taipei.
    parsed = parse_timestamp(1781760623530000)

    assert parsed == ts("2026-06-18T13:30:23.530000+08:00")


def test_parse_fubon_books_quote_reads_top_level_bid_ask_and_sizes() -> None:
    fetched = parse_fubon_books_quote(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "CCFG6",
                "time": 1781743501000000,
                "bids": [{"price": 2409.0, "size": 12}],
                "asks": [{"price": 2411.0, "size": 8}],
            },
        }
    )

    assert fetched is not None
    assert fetched.symbol == "CCFG6"
    assert fetched.price == 2410.0
    assert fetched.bid == 2409.0
    assert fetched.ask == 2411.0
    assert fetched.bid_size == 12.0
    assert fetched.ask_size == 8.0


def test_fubon_books_cache_fetch_quote_returns_latest_book() -> None:
    provider = FubonCcfMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    websocket = FakeFubonWebSocket()
    provider.websocket = websocket

    provider.ensure_books_subscription("CCFG6", after_hours=True)
    websocket.emit(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "CCFG6",
                "time": "2026-06-18T02:45:01+08:00",
                "bids": [{"price": 2409.0, "size": 3}],
                "asks": [{"price": 2411.0, "size": 4}],
            },
        }
    )

    fetched = provider.fetch_quote("CCFG6")

    assert websocket.connected
    assert websocket.subscriptions == [
        {"channel": "books", "symbol": "CCFG6", "afterHours": True}
    ]
    assert fetched.bid == 2409.0
    assert fetched.ask == 2411.0
    assert fetched.bid_size == 3.0
    assert fetched.ask_size == 4.0


def test_fubon_books_subscription_can_unsubscribe_old_symbol() -> None:
    provider = FubonCcfMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    websocket = FakeFubonWebSocket()
    provider.websocket = websocket

    provider.ensure_books_subscription("CCFG6", after_hours=False)
    websocket.emit(
        {
            "event": "subscribed",
            "channel": "books",
            "data": {"symbol": "CCFG6", "channel": "books", "id": "sub-1"},
        }
    )
    provider.unsubscribe_books("CCFG6")

    assert websocket.unsubscriptions == [{"id": "sub-1"}]


def test_fubon_ccf_candles_merge_regular_and_afterhours_sessions() -> None:
    provider = FubonCcfMarketData(None)
    intraday = FakeFubonIntraday(
        {
            "candles_regular": {
                "data": [
                    {"date": "2026-06-18T03:45:00+08:00", "close": 100.0},
                ]
            },
            "candles_afterhours": {
                "data": [
                    {"date": "2026-06-18T17:25:00+08:00", "close": 101.0},
                    {"date": "2026-06-19T00:04:00+08:00", "close": 102.0},
                ]
            },
        }
    )
    provider.intraday = intraday

    frame = provider.fetch_1m(
        "CCFG6",
        ts("2026-06-18T03:45:00+08:00"),
        ts("2026-06-19T00:04:00+08:00"),
    )

    assert frame["timestamp"].tolist() == [
        pd.Timestamp("2026-06-18T03:45:00+08:00"),
        pd.Timestamp("2026-06-18T17:25:00+08:00"),
        pd.Timestamp("2026-06-19T00:04:00+08:00"),
    ]
    assert intraday.candle_calls == [
        {"symbol": "CCFG6", "timeframe": "1"},
        {"symbol": "CCFG6", "timeframe": "1", "session": "afterhours"},
    ]


def test_fubon_books_restart_clears_cache_disconnects_and_resubscribes() -> None:
    provider = FubonCcfMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    websocket = FakeFubonWebSocket()
    provider.websocket = websocket

    provider.ensure_books_subscription("CCFG6", after_hours=True)
    websocket.emit(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "CCFG6",
                "time": "2026-06-18T02:45:01+08:00",
                "bids": [{"price": 2409.0, "size": 3}],
                "asks": [{"price": 2411.0, "size": 4}],
            },
        }
    )

    provider.restart_books_session("CCFG6", after_hours=False)

    assert websocket.disconnected
    assert websocket.connect_calls == 2
    assert websocket.subscriptions == [
        {"channel": "books", "symbol": "CCFG6", "afterHours": True},
        {"channel": "books", "symbol": "CCFG6", "afterHours": False},
    ]
    assert provider._latest_books == {}
    assert provider._book_subscribed_symbols == {"CCFG6"}


def test_fubon_quote_rest_diagnostics_does_not_fill_book_fields() -> None:
    provider = FubonCcfMarketData(None, book_wait_timeout_seconds=0.0)
    provider.intraday = FakeFubonIntraday({"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}})
    provider.websocket = FakeFubonWebSocket()

    fetched = provider.fetch_quote("CCFG6")

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


def test_taifex_ccf_trade_downloader_aggregates_tick_csv_to_1m(tmp_path) -> None:
    zip_payload = make_taifex_zip(
        "\n".join(
            [
                "成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),近月價格,遠月價格,開盤集合競價 ",
                "20260617,CCF    ,202607     ,172500,2415,150,-,-,*",
                "20260617,CCF    ,202607     ,172513,2420,2,-,-, ",
                "20260617,CCF    ,202608     ,172530,2500,2,-,-, ",
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

    frame = TaifexCcfTradeDownloader(
        tmp_path / "cache",
        http_get=http_get,
    ).fetch_1m(
        "CCFG6",
        ts("2026-06-17T17:25:00+08:00"),
        ts("2026-06-17T17:26:00+08:00"),
    )

    assert len(frame) == 1
    assert frame.iloc[0]["timestamp"] == pd.Timestamp("2026-06-17T17:25:00+08:00")
    assert frame.iloc[0]["close"] == 2420.0


def test_warmup_builder_combines_ccf_sources_and_forward_fills(tmp_path) -> None:
    config = small_live_config(tmp_path)
    fallback = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:47:00+08:00", 102.0),
            ]
        )
    )
    intraday = FakeCcfProvider(rows([("2026-06-18T02:47:00+08:00", 103.0)]))
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 20.0),
                ("2026-06-18T02:46:00+08:00", 20.5),
                ("2026-06-18T02:47:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    bars = WarmupBuilder(
        live_config=config.live,
        ccf_intraday_provider=intraday,
        ccf_fallback_provider=fallback,
        umc_provider=umc,
        usd_twd_provider=usd,
    ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:42+08:00"))

    assert len(bars) == 3
    assert [bar.timestamp for bar in bars] == [
        ts("2026-06-18T02:45:00+08:00"),
        ts("2026-06-18T02:46:00+08:00"),
        ts("2026-06-18T02:47:00+08:00"),
    ]
    assert bars[1].ccf_close is None
    assert bars[1].ccf_close_filled == 100.0
    assert bars[2].ccf_close_filled == 103.0
    assert bars[2].umc_twd_fair == 21.0 * 30.0 / 5.0
    assert bars[2].spread == pytest.approx((bars[2].umc_twd_fair - 103.0) / (bars[2].umc_twd_fair + 103.0) * 200.0)


def test_warmup_builder_does_not_fetch_fallback_when_intraday_is_sufficient(
    tmp_path,
) -> None:
    config = small_live_config(tmp_path)
    intraday_rows = rows(
        [
            ("2026-06-18T02:45:00+08:00", 100.0),
            ("2026-06-18T02:46:00+08:00", 101.0),
            ("2026-06-18T02:47:00+08:00", 102.0),
        ]
    )
    intraday = FakeCcfProvider(intraday_rows)
    fallback = FakeCcfProvider(intraday_rows)
    umc = FakeOhlcvProvider(intraday_rows)
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    bars = WarmupBuilder(
        live_config=config.live,
        ccf_intraday_provider=intraday,
        ccf_fallback_provider=fallback,
        umc_provider=umc,
        usd_twd_provider=usd,
    ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:42+08:00"))

    assert len(bars) == 3
    assert intraday.fetch_1m_calls
    assert fallback.fetch_1m_calls == []


def test_expected_warmup_index_is_anchored_to_current_session() -> None:
    warmup_index, session_index = build_ccf_expected_warmup_index(
        start=ts("2026-06-18T03:40:00+08:00"),
        end=ts("2026-06-19T00:04:00+08:00"),
        count=3,
    )

    assert warmup_index.tolist() == [
        pd.Timestamp("2026-06-19T00:02:00+08:00"),
        pd.Timestamp("2026-06-19T00:03:00+08:00"),
        pd.Timestamp("2026-06-19T00:04:00+08:00"),
    ]
    # 03:45 is inside Wednesday's US session, which runs to Taipei 04:00.
    assert pd.Timestamp("2026-06-18T03:45:00+08:00") in session_index
    # 17:25 opens the TAIFEX night session, but NYSE does not open until 21:30.
    # The pair's index is the INTERSECTION, so those four hours are not in it --
    # a spread built from a frozen UMC price there would be fiction.
    assert pd.Timestamp("2026-06-18T17:25:00+08:00") not in session_index
    assert pd.Timestamp("2026-06-18T21:29:00+08:00") not in session_index
    assert pd.Timestamp("2026-06-18T21:30:00+08:00") in session_index


def test_warmup_builder_refuses_wholly_missing_current_night_session(
    tmp_path,
) -> None:
    config = small_live_config(tmp_path)
    day_only = rows(
        [
            ("2026-06-18T03:43:00+08:00", 100.0),
            ("2026-06-18T03:44:00+08:00", 101.0),
            ("2026-06-18T03:45:00+08:00", 102.0),
        ]
    )

    with pytest.raises(RuntimeError, match="expected warmup window"):
        WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=FakeCcfProvider(day_only),
            ccf_fallback_provider=FakeCcfProvider(day_only),
            umc_provider=FakeOhlcvProvider(pd.DataFrame()),
            usd_twd_provider=FakeOhlcvProvider(pd.DataFrame()),
        ).build(
            ccf_symbol="CCF202607",
            end=ts("2026-06-18T19:42:00+08:00"),
        )


def test_warmup_builder_refuses_when_forward_fill_ratio_too_high(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(
        config,
        live=replace(config.live, warmup_forward_fill_max_ratio=0.2),
    )
    # Same data as the success case (1 of 3 minutes forward-filled = 0.33 > 0.2).
    fallback = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:47:00+08:00", 102.0),
            ]
        )
    )
    intraday = FakeCcfProvider(rows([("2026-06-18T02:47:00+08:00", 103.0)]))
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 20.0),
                ("2026-06-18T02:46:00+08:00", 20.5),
                ("2026-06-18T02:47:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="forward-fill ratio"):
        WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=intraday,
            ccf_fallback_provider=fallback,
            umc_provider=umc,
            usd_twd_provider=usd,
        ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:42+08:00"))


def test_warmup_builder_refuses_stale_trailing_ccf_data(tmp_path) -> None:
    config = small_live_config(tmp_path)
    stale_ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:46:00+08:00", 101.0),
            ]
        )
    )
    current_rows = rows(
        [
            ("2026-06-18T02:45:00+08:00", 20.0),
            ("2026-06-18T02:46:00+08:00", 20.0),
            ("2026-06-18T02:47:00+08:00", 20.0),
            ("2026-06-18T02:48:00+08:00", 20.0),
            ("2026-06-18T02:49:00+08:00", 20.0),
            ("2026-06-18T02:50:00+08:00", 20.0),
            ("2026-06-18T02:51:00+08:00", 20.0),
            ("2026-06-18T02:52:00+08:00", 20.0),
        ]
    )

    with pytest.raises(RuntimeError, match="latest actual bar is stale"):
        WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=stale_ccf,
            ccf_fallback_provider=None,
            umc_provider=FakeOhlcvProvider(current_rows),
            usd_twd_provider=FakeOhlcvProvider(current_rows),
        ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:53:00+08:00"))


def test_ccf_warmup_source_report_tracks_precedence_and_quality() -> None:
    report = build_ccf_warmup_source_report(
        [
            (
                "taifex",
                rows(
                    [
                        ("2026-06-18T02:44:00+08:00", 99.0),
                        ("2026-06-18T02:45:00+08:00", 100.0),
                        ("2026-06-18T02:47:00+08:00", 102.0),
                    ]
                ),
            ),
            ("fubon", rows([("2026-06-18T02:47:00+08:00", 103.0)])),
        ],
        start_minute=ts("2026-06-18T02:45:00+08:00"),
        end_minute=ts("2026-06-18T02:47:00+08:00"),
        ccf_fetch_start=ts("2026-06-18T02:44:00+08:00"),
    )

    assert report.null_count == 0
    assert report.mismatch_count == 1
    assert report.max_abs_diff == 1.0
    assert report.frame.loc[0, "ccf_close_filled"] == 100.0
    assert report.frame.loc[1, "ccf_close_filled"] == 100.0
    assert report.frame.loc[1, "source_used"] == "forward_fill"
    assert report.frame.loc[2, "merged_ccf_close"] == 103.0
    assert report.frame.loc[2, "source_used"] == "fubon"


def test_warmup_builder_uses_prior_ccf_close_to_seed_forward_fill(tmp_path) -> None:
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 99.0),
                ("2026-06-18T02:46:00+08:00", 101.0),
            ]
        )
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 20.0),
                ("2026-06-18T02:46:00+08:00", 20.0),
                ("2026-06-18T02:47:00+08:00", 20.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    bars = WarmupBuilder(
        live_config=config.live,
        ccf_intraday_provider=ccf,
        ccf_fallback_provider=None,
        umc_provider=umc,
        usd_twd_provider=usd,
    ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:00+08:00"))

    assert bars[0].ccf_close == 99.0
    assert bars[0].ccf_close_filled == 99.0
    assert bars[1].ccf_close_filled == 101.0


def test_warmup_builder_fails_when_initial_ccf_cannot_be_filled(tmp_path) -> None:
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(rows([("2026-06-18T02:46:00+08:00", 101.0)]))
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 20.0),
                ("2026-06-18T02:46:00+08:00", 20.0),
                ("2026-06-18T02:47:00+08:00", 20.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="cannot forward-fill"):
        WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=ccf,
            ccf_fallback_provider=None,
            umc_provider=umc,
            usd_twd_provider=usd,
        ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:00+08:00"))


def test_warmup_builder_fails_when_umc_or_usd_is_missing_mid_window(tmp_path) -> None:
    """An interior hole is data that is gone, and it must stay fatal.

    Every mean and std in the window is computed over these minutes, so
    papering over one would corrupt the z-score the strategy trades on.
    """
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:46:00+08:00", 101.0),
                ("2026-06-18T02:47:00+08:00", 102.0),
            ]
        )
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 20.0),
                ("2026-06-18T02:47:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="missing minutes"):
        WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=ccf,
            ccf_fallback_provider=None,
            umc_provider=umc,
            usd_twd_provider=usd,
        ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:00+08:00"))


def test_warmup_builder_trims_trailing_minutes_the_vendor_has_not_published(
    tmp_path,
) -> None:
    """Twelve Data writes its 1m series minutes late; late is not lost.

    The window reaches to now-1min, so a restart routinely asks for bars the
    vendor has not published yet. On 2026-08-07 that killed a live-execute
    restart at 22:36:27 over a 22:35 bar which was present by 22:39:56. Drop
    the tail rather than the whole warmup.
    """
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:46:00+08:00", 101.0),
                ("2026-06-18T02:47:00+08:00", 102.0),
            ]
        )
    )
    published = [
        ("2026-06-18T02:45:00+08:00", 20.0),
        ("2026-06-18T02:46:00+08:00", 21.0),
    ]
    umc = FakeOhlcvProvider(rows(published))
    usd = FakeOhlcvProvider(
        rows([(stamp, 30.0) for stamp, _ in published])
    )

    bars = WarmupBuilder(
        live_config=config.live,
        ccf_intraday_provider=ccf,
        ccf_fallback_provider=None,
        umc_provider=umc,
        usd_twd_provider=usd,
    ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T02:48:00+08:00"))

    assert [bar.timestamp for bar in bars] == [
        ts("2026-06-18T02:45:00+08:00"),
        ts("2026-06-18T02:46:00+08:00"),
    ]
    # The surviving bars are complete -- trimming must not leave a NaN behind.
    assert all(bar.umc_twd_fair == bar.umc_twd_fair for bar in bars)
    assert all(bar.spread == bar.spread for bar in bars)


def test_warmup_builder_refuses_a_trailing_gap_too_big_to_be_vendor_lag(
    tmp_path,
) -> None:
    """Past ~1.5x the vendor's worst measured lag it is an outage, not lag."""
    base = small_live_config(tmp_path)
    config = replace(base, live=replace(base.live, warmup_minutes=15))
    minutes = [f"2026-06-18T02:{45 + offset:02d}:00+08:00" for offset in range(15)]
    ccf = FakeCcfProvider(rows([(stamp, 100.0) for stamp in minutes]))
    umc = FakeOhlcvProvider(rows([(stamp, 20.0) for stamp in minutes[:3]]))
    usd = FakeOhlcvProvider(rows([(stamp, 30.0) for stamp in minutes[:3]]))

    with pytest.raises(RuntimeError, match="more than vendor lag explains"):
        WarmupBuilder(
            live_config=config.live,
            ccf_intraday_provider=ccf,
            ccf_fallback_provider=None,
            umc_provider=umc,
            usd_twd_provider=usd,
        ).build(ccf_symbol="CCF202607", end=ts("2026-06-18T03:00:00+08:00"))


def test_live_minute_bar_builder_finalizes_on_minute_crossing() -> None:
    builder = LiveMinuteBarBuilder(stale_seconds=10.0, max_leg_timestamp_skew_seconds=10.0)
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:55+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:55+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T02:45:55+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    assert builder.update(first, ts("2026-06-18T02:45:55+08:00")) is None
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.bar is not None
    assert result.bar.timestamp == ts("2026-06-18T02:45:00+08:00")
    assert result.bar.ccf_close_filled == 100.0


def test_live_minute_bar_builder_allows_ccf_forward_fill_but_skips_stale_umc() -> None:
    builder = LiveMinuteBarBuilder(stale_seconds=10.0, max_leg_timestamp_skew_seconds=10.0)
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:55+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:00+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T02:45:55+08:00", 30.0),
    )
    stale = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:55+08:00"))
    result = builder.update(stale, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason == "market_data_stale"


def test_fx_gets_its_own_staleness_budget() -> None:
    """REGRESSION: the first CCF/UMC dry run skipped EVERY minute with
    `stale_usd_twd`, so the pair could never form a bar, never score a z, and
    never trade.

    One `stale_seconds` covered both UMC and USD/TWD. 10s is right for an
    exchange feed and impossible for a rate served from a 300s vendor cache.
    `fx_stale_seconds` already existed in config and was wired to nothing.
    """
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        max_leg_timestamp_skew_seconds=10.0,
    )
    builder.last_ccf_close = 99.0
    # FX is four minutes old: far past the exchange budget, well inside its own.
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:59+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T02:42:00+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:42:00+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason is None
    assert result.bar is not None


def test_fx_is_still_rejected_once_it_passes_its_own_budget() -> None:
    """The budget is longer, not absent. An hour-old rate is a real fault."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        max_leg_timestamp_skew_seconds=10.0,
    )
    builder.last_ccf_close = 99.0
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:59+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T01:30:00+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T01:30:00+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason == "market_data_stale"
    assert result.payload["source"] == "usd_twd"
    assert result.payload["budget_seconds"] == 600.0


def test_fx_age_does_not_count_as_leg_skew() -> None:
    """The other half of the same bug. The skew gate asks whether the two LEGS
    are priced from the same moment; FX is not a leg, it only converts one of
    them. Including it made a healthy pair look skewed by exactly the vendor's
    cache age."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        max_leg_timestamp_skew_seconds=10.0,
    )
    builder.last_ccf_close = 99.0
    # CCF and UMC are two seconds apart; FX is four minutes behind both.
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:57+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T02:42:00+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:42:00+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason is None


def test_real_leg_skew_between_ccf_and_umc_still_rejects() -> None:
    """Exempting FX must not disarm the gate for the two legs it exists for."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=60.0,
        usd_twd_stale_seconds=600.0,
        max_leg_timestamp_skew_seconds=10.0,
    )
    builder.last_ccf_close = 99.0
    # The finalized minute is the FIRST set: update() closes out the previous
    # minute before adopting the new quotes. CCF sits 28s behind UMC here, with
    # both inside the 60s staleness budget, so only the skew gate can catch it.
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:31+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T02:45:59+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason == "leg_timestamp_skew"


def test_a_stale_trade_price_is_refused_even_with_a_live_book() -> None:
    """REGRESSION: the bar's close is a TRADE price while the quote is stamped
    with the BOOK clock, so the staleness gate stopped bounding the number the
    bar is actually built from. A thirty-second-old print read as zero seconds
    old and entered the rolling mean and std."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        umc_trade_stale_seconds=60.0,
        max_leg_timestamp_skew_seconds=30.0,
    )
    builder.last_ccf_close = 99.0
    # Book is current; the last trade is five minutes old.
    stale_trade = replace(
        quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        price_timestamp=ts("2026-06-18T02:41:00+08:00"),
    )
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:59+08:00", 100.0),
        umc=stale_trade,
        usd_twd=quote("usd", "2026-06-18T02:45:59+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason == "stale_trade_price"
    assert result.payload["source"] == "umc"


def test_a_quiet_minute_still_builds_from_its_last_trade() -> None:
    """The other side of the same budget: the correct close of a minute with no
    prints IS the last trade in it, so a 40s-old trade must not be refused."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        umc_trade_stale_seconds=60.0,
        max_leg_timestamp_skew_seconds=30.0,
    )
    builder.last_ccf_close = 99.0
    quiet = replace(
        quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        price_timestamp=ts("2026-06-18T02:45:20+08:00"),
    )
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:45:40+08:00", 100.0),
        umc=quiet,
        usd_twd=quote("usd", "2026-06-18T02:45:59+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason is None
    assert result.bar is not None


def test_skew_compares_venue_clocks_not_the_local_receive_time() -> None:
    """UMC's quote timestamp is a local receive clock; CCF's is an exchange
    print. Comparing those measures clock offset, not leg skew. The gate now
    compares PRICE timestamps, which are both venue-sourced."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=60.0,
        usd_twd_stale_seconds=600.0,
        umc_trade_stale_seconds=120.0,
        max_leg_timestamp_skew_seconds=10.0,
    )
    builder.last_ccf_close = 99.0
    # Receive clocks agree; the underlying prints are 40s apart.
    skewed = replace(
        quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        price_timestamp=ts("2026-06-18T02:45:59+08:00"),
    )
    first = LiveQuoteSet(
        ccf=replace(
            quote("ccf", "2026-06-18T02:45:59+08:00", 100.0),
            price_timestamp=ts("2026-06-18T02:45:19+08:00"),
        ),
        umc=skewed,
        usd_twd=quote("usd", "2026-06-18T02:45:59+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.skipped_reason == "leg_timestamp_skew"
    assert result.payload["skew_seconds"] == pytest.approx(40.0)


def test_live_minute_bar_builder_forward_fills_stale_ccf_quote() -> None:
    builder = LiveMinuteBarBuilder(stale_seconds=10.0, max_leg_timestamp_skew_seconds=10.0)
    builder.last_ccf_close = 99.0
    first = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:44:00+08:00", 100.0),
        umc=quote("umc", "2026-06-18T02:45:59+08:00", 20.0),
        usd_twd=quote("usd", "2026-06-18T02:45:59+08:00", 30.0),
    )
    second = LiveQuoteSet(
        ccf=quote("ccf", "2026-06-18T02:46:01+08:00", 101.0),
        umc=quote("umc", "2026-06-18T02:46:01+08:00", 21.0),
        usd_twd=quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
    )

    builder.update(first, ts("2026-06-18T02:45:59+08:00"))
    result = builder.update(second, ts("2026-06-18T02:46:01+08:00"))

    assert result is not None
    assert result.bar is not None
    assert result.bar.ccf_close is None
    assert result.bar.ccf_close_filled == 99.0


def test_live_runtime_minute_boundaries_and_no_signal_bar(tmp_path) -> None:
    config = small_live_config(tmp_path)
    warmup_ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 100.0),
                ("2026-06-18T02:44:00+08:00", 100.0),
            ]
        )
    )
    warmup_umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.0),
                ("2026-06-18T02:44:00+08:00", 20.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        )
    )
    WarmupRunner(
        config,
        ccf_provider=warmup_ccf,
        ccf_fallback_provider=None,
        umc_provider=warmup_umc,
        usd_twd_provider=warmup_usd,
    ).run(reset_store=True, end=ts("2026-06-18T02:45:00+08:00"))

    clocks = iter(
        [
            ts("2026-06-18T02:45:30+08:00"),
            ts("2026-06-18T02:45:59+08:00"),
            ts("2026-06-18T02:46:00+08:00"),
            ts("2026-06-18T02:46:01+08:00"),
            ts("2026-06-18T02:46:01+08:00"),
        ]
    )
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 100.0),
                ("2026-06-18T02:44:00+08:00", 100.0),
            ]
        ),
        quotes=[
            quote("ccf", "2026-06-18T02:45:59+08:00", 100.0, bid=99.9, ask=100.1),
            quote("ccf", "2026-06-18T02:46:00+08:00", 100.0, bid=99.9, ask=100.1),
            quote("ccf", "2026-06-18T02:46:01+08:00", 100.0, bid=99.9, ask=100.1),
        ]
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.0),
                ("2026-06-18T02:44:00+08:00", 20.0),
            ]
        ),
        quotes=[
            quote("umc", "2026-06-18T02:45:59+08:00", 20.0, bid=19.99, ask=20.01),
            quote("umc", "2026-06-18T02:46:00+08:00", 20.0, bid=19.99, ask=20.01),
            quote("umc", "2026-06-18T02:46:01+08:00", 20.0, bid=19.99, ask=20.01),
        ],
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        ),
        quotes=[
            quote("usd", "2026-06-18T02:45:59+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-18T02:46:00+08:00", 30.0, bid=29.99, ask=30.01),
            quote("usd", "2026-06-18T02:46:01+08:00", 30.0, bid=29.99, ask=30.01),
        ],
    )
    terminal_output = io.StringIO()
    reporter = LiveTerminalReporter(terminal_output, color=False)

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=lambda: next(clocks),
        sleeper=lambda _: None,
        reporter=reporter,
    ).run(resume=True, max_iterations=3)

    assert result.bars_processed == 1
    output = terminal_output.getvalue()
    assert output.count("LIVE") == 3
    assert "02:45 BAR  " in output
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


def test_live_runtime_terminal_reporter_warns_on_stale_minute(tmp_path) -> None:
    config = small_live_config(tmp_path)
    warmup_ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 100.0),
                ("2026-06-18T02:44:00+08:00", 100.0),
            ]
        )
    )
    warmup_umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.0),
                ("2026-06-18T02:44:00+08:00", 20.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        )
    )
    WarmupRunner(
        config,
        ccf_provider=warmup_ccf,
        ccf_fallback_provider=None,
        umc_provider=warmup_umc,
        usd_twd_provider=warmup_usd,
    ).run(reset_store=True, end=ts("2026-06-18T02:45:00+08:00"))

    clocks = iter(
        [
            ts("2026-06-18T02:45:30+08:00"),
            ts("2026-06-18T02:45:59+08:00"),
            ts("2026-06-18T02:46:01+08:00"),
            ts("2026-06-18T02:46:01+08:00"),
        ]
    )
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 100.0),
                ("2026-06-18T02:44:00+08:00", 100.0),
            ]
        ),
        quotes=[
            quote("ccf", "2026-06-18T02:45:59+08:00", 100.0),
            quote("ccf", "2026-06-18T02:46:01+08:00", 100.0),
        ]
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.0),
                ("2026-06-18T02:44:00+08:00", 20.0),
            ]
        ),
        quotes=[
            quote("umc", "2026-06-18T02:45:40+08:00", 20.0),
            quote("umc", "2026-06-18T02:46:01+08:00", 20.0),
        ],
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        ),
        quotes=[
            quote("usd", "2026-06-18T02:45:59+08:00", 30.0),
            quote("usd", "2026-06-18T02:46:01+08:00", 30.0),
        ],
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=lambda: next(clocks),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(resume=True, max_iterations=2)

    assert result.bars_processed == 0
    assert result.skipped_minutes == 1
    assert "WARN stale_umc skipped_minute" in terminal_output.getvalue()


def test_live_dry_run_records_simulated_entry_and_opens_position(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, strategy=replace(config.strategy, entry_z=1.0))
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T02:45:30+08:00",
            "2026-06-18T02:45:59+08:00",
            "2026-06-18T02:46:01+08:00",
            "2026-06-18T02:46:59+08:00",
            "2026-06-18T02:47:01+08:00",
        ]
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T02:45:00+08:00",
                "2026-06-18T02:45:30+08:00",
                "2026-06-18T02:45:59+08:00",
                "2026-06-18T02:46:01+08:00",
                "2026-06-18T02:46:59+08:00",
                "2026-06-18T02:47:01+08:00",
                "2026-06-18T02:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(reset_store=True, max_iterations=5)

    assert result.bars_processed == 2
    assert result.plans_recorded == 1
    output = terminal_output.getvalue()
    assert "SHORT entry_fill" in output
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
        "IBKR_ALLOW_LIVE_ORDER",
    ):
        monkeypatch.setenv(name, "1")
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T02:45:30+08:00",
            "2026-06-18T02:45:59+08:00",
            "2026-06-18T02:46:01+08:00",
            "2026-06-18T02:46:59+08:00",
            "2026-06-18T02:47:01+08:00",
        ]
    )
    ccf_adapter = FakeLiveExecutionAdapter(BrokerName.FUBON_CCF)
    umc_adapter = FakeLiveExecutionAdapter(BrokerName.IBKR_UMC)
    terminal_output = io.StringIO()

    result = LiveExecuteRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        fubon_adapter=ccf_adapter,
        umc_adapter=umc_adapter,
        readonly_brokers=(
            FixedPositionReadOnlyBroker(
                broker=BrokerName.FUBON_CCF,
                symbol="CCFG6",
                quantity=100.0,
                fetched_at=ts("2026-06-18T02:47:01+08:00"),
            ),
            FixedPositionReadOnlyBroker(
                broker=BrokerName.IBKR_UMC,
                symbol="UMC",
                quantity=-(1_000_000.0 / (120.0 * 5.0)),
                fetched_at=ts("2026-06-18T02:47:01+08:00"),
            ),
        ),
        clock=dry_run_clock(
            [
                "2026-06-18T02:45:00+08:00",
                "2026-06-18T02:45:30+08:00",
                "2026-06-18T02:45:59+08:00",
                "2026-06-18T02:46:01+08:00",
                "2026-06-18T02:46:59+08:00",
                "2026-06-18T02:47:01+08:00",
                "2026-06-18T02:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(reset_store=True, max_iterations=5)

    assert result.bars_processed == 2
    assert result.plans_recorded == 1
    assert len(ccf_adapter.plans) == 1
    assert len(umc_adapter.plans) == 1
    output = terminal_output.getvalue()
    assert "EVENT warmup_auto start" in output
    assert "EVENT live_execution filled" in output
    assert "EVENT post_trade_reconciliation matched" in output
    assert "SHORT entry_fill" in output

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
        assert state.strategy.position_direction == Direction.SHORT_UMC_LONG_CCF
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


def _live_execute_resume_brokers(*, ccf_quantity: float, umc_quantity: float, at: str):
    return (
        FixedPositionReadOnlyBroker(
            broker=BrokerName.FUBON_CCF,
            symbol="CCFG6",
            quantity=ccf_quantity,
            fetched_at=ts(at),
        ),
        FixedPositionReadOnlyBroker(
            broker=BrokerName.IBKR_UMC,
            symbol="UMC",
            quantity=umc_quantity,
            fetched_at=ts(at),
        ),
    )


def _run_live_execute_entry_to_open(config) -> None:
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T02:45:30+08:00",
            "2026-06-18T02:45:59+08:00",
            "2026-06-18T02:46:01+08:00",
            "2026-06-18T02:46:59+08:00",
            "2026-06-18T02:47:01+08:00",
        ]
    )
    LiveExecuteRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        fubon_adapter=FakeLiveExecutionAdapter(BrokerName.FUBON_CCF),
        umc_adapter=FakeLiveExecutionAdapter(BrokerName.IBKR_UMC),
        readonly_brokers=_live_execute_resume_brokers(
            ccf_quantity=100.0,
            umc_quantity=-(1_000_000.0 / (120.0 * 5.0)),
            at="2026-06-18T02:47:01+08:00",
        ),
        clock=dry_run_clock(
            [
                "2026-06-18T02:45:00+08:00",
                "2026-06-18T02:45:30+08:00",
                "2026-06-18T02:45:59+08:00",
                "2026-06-18T02:46:01+08:00",
                "2026-06-18T02:46:59+08:00",
                "2026-06-18T02:47:01+08:00",
                "2026-06-18T02:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(reset_store=True, max_iterations=5)


def _resume_live_execute(config, *, readonly):
    fresh_ccf = rows(
        [
            ("2026-06-18T02:44:00+08:00", 100.0),
            ("2026-06-18T02:45:00+08:00", 100.0),
            ("2026-06-18T02:46:00+08:00", 100.0),
        ]
    )
    fresh_umc = rows(
        [
            ("2026-06-18T02:44:00+08:00", 20.0),
            ("2026-06-18T02:45:00+08:00", 20.0),
            ("2026-06-18T02:46:00+08:00", 20.0),
        ]
    )
    fresh_usd = rows(
        [
            ("2026-06-18T02:44:00+08:00", 30.0),
            ("2026-06-18T02:45:00+08:00", 30.0),
            ("2026-06-18T02:46:00+08:00", 30.0),
        ]
    )
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T02:47:30+08:00",
            "2026-06-18T02:47:59+08:00",
            "2026-06-18T02:48:01+08:00",
        ],
        ccf_rows=fresh_ccf,
        umc_rows=fresh_umc,
        usd_rows=fresh_usd,
    )
    terminal = io.StringIO()
    LiveExecuteRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        fubon_adapter=FakeLiveExecutionAdapter(BrokerName.FUBON_CCF),
        umc_adapter=FakeLiveExecutionAdapter(BrokerName.IBKR_UMC),
        readonly_brokers=readonly,
        clock=dry_run_clock(
            [
                "2026-06-18T02:47:02+08:00",
                "2026-06-18T02:47:30+08:00",
                "2026-06-18T02:47:59+08:00",
                "2026-06-18T02:48:01+08:00",
                "2026-06-18T02:48:02+08:00",
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
        "IBKR_ALLOW_LIVE_ORDER",
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
            ccf_quantity=100.0,
            umc_quantity=-(1_000_000.0 / (120.0 * 5.0)),
            at="2026-06-18T02:47:30+08:00",
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
            ccf_quantity=0.0,
            umc_quantity=0.0,
            at="2026-06-18T02:47:30+08:00",
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


def test_live_execute_resume_keeps_position_when_broker_unreachable(
    tmp_path, monkeypatch
) -> None:
    config = _live_execute_resume_config(tmp_path, monkeypatch)
    _run_live_execute_entry_to_open(config)

    # Read-only API is unreachable at restart.  Query transport failure is not
    # evidence that the restored broker position is wrong: preserve the open
    # position and close only the new-entry gate until a snapshot succeeds.
    _resume_live_execute(
        config,
        readonly=(
            _RaisingReadOnlyBroker(BrokerName.FUBON_CCF),
            FixedPositionReadOnlyBroker(
                broker=BrokerName.IBKR_UMC,
                symbol="UMC",
                quantity=-(1_000_000.0 / (120.0 * 5.0)),
                fetched_at=ts("2026-06-18T02:47:30+08:00"),
            ),
        ),
    )

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.OPEN
    finally:
        store.close()


def test_live_dry_run_resume_does_not_duplicate_recorded_intent(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, strategy=replace(config.strategy, entry_z=1.0))
    first_ccf, first_umc, first_usd = dry_run_quote_providers(
        [
            "2026-06-18T02:45:30+08:00",
            "2026-06-18T02:45:59+08:00",
            "2026-06-18T02:46:01+08:00",
            "2026-06-18T02:46:59+08:00",
            "2026-06-18T02:47:01+08:00",
        ]
    )

    first_result = LiveDryRunRunner(
        config,
        ccf_provider=first_ccf,
        umc_provider=first_umc,
        usd_twd_provider=first_usd,
        clock=dry_run_clock(
            [
                "2026-06-18T02:45:00+08:00",
                "2026-06-18T02:45:30+08:00",
                "2026-06-18T02:45:59+08:00",
                "2026-06-18T02:46:01+08:00",
                "2026-06-18T02:46:59+08:00",
                "2026-06-18T02:47:01+08:00",
                "2026-06-18T02:47:02+08:00",
            ]
        ),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(reset_store=True, max_iterations=5)

    assert first_result.plans_recorded == 1

    resume_ccf = rows(
        [
            ("2026-06-18T02:44:00+08:00", 100.0),
            ("2026-06-18T02:45:00+08:00", 100.0),
            ("2026-06-18T02:46:00+08:00", 100.0),
        ]
    )
    resume_umc = rows(
        [
            ("2026-06-18T02:44:00+08:00", 20.0),
            ("2026-06-18T02:45:00+08:00", 20.0),
            ("2026-06-18T02:46:00+08:00", 20.0),
        ]
    )
    resume_usd = rows(
        [
            ("2026-06-18T02:44:00+08:00", 30.0),
            ("2026-06-18T02:45:00+08:00", 30.0),
            ("2026-06-18T02:46:00+08:00", 30.0),
        ]
    )
    second_ccf, second_umc, second_usd = dry_run_quote_providers(
        [
            "2026-06-18T02:47:30+08:00",
            "2026-06-18T02:47:59+08:00",
            "2026-06-18T02:48:01+08:00",
        ],
        ccf_rows=resume_ccf,
        umc_rows=resume_umc,
        usd_rows=resume_usd,
    )
    second_result = LiveDryRunRunner(
        config,
        ccf_provider=second_ccf,
        umc_provider=second_umc,
        usd_twd_provider=second_usd,
        clock=dry_run_clock(
            [
                "2026-06-18T02:47:02+08:00",
                "2026-06-18T02:47:30+08:00",
                "2026-06-18T02:47:59+08:00",
                "2026-06-18T02:48:01+08:00",
                "2026-06-18T02:48:02+08:00",
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


def test_reconnect_ccf_provider_if_supported_relogins_and_stays_safe() -> None:
    from lux_trader.runtime.live.contracts import reconnect_ccf_provider_if_supported

    class _ReconProvider:
        def __init__(self, exc: Exception | None = None) -> None:
            self.calls = 0
            self.exc = exc

        def reconnect(self) -> None:
            self.calls += 1
            if self.exc is not None:
                raise self.exc

    when = ts("2026-06-18T02:45:00+08:00")

    out = io.StringIO()
    provider = _ReconProvider()
    reconnect_ccf_provider_if_supported(provider, LiveTerminalReporter(out, color=False), when)
    assert provider.calls == 1
    assert "reconnect_login" in out.getvalue()

    out_fail = io.StringIO()
    raising = _ReconProvider(exc=RuntimeError("login boom"))
    reconnect_ccf_provider_if_supported(
        raising, LiveTerminalReporter(out_fail, color=False), when
    )
    assert raising.calls == 1  # attempted
    assert "reconnect_failed" in out_fail.getvalue()  # caught, not propagated

    # A provider without reconnect support must be a no-op, never an error.
    reconnect_ccf_provider_if_supported(object(), LiveTerminalReporter(io.StringIO(), color=False), when)


def test_live_dry_run_survives_contract_resolution_failure(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, strategy=replace(config.strategy, entry_z=1.0))
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T02:45:30+08:00",
            "2026-06-18T02:45:59+08:00",
            "2026-06-18T02:46:01+08:00",
            "2026-06-18T02:46:59+08:00",
            "2026-06-18T02:47:01+08:00",
        ]
    )
    # Startup resolves the contract once successfully; every later per-bar
    # re-resolution raises (mimicking a Fugle token-expired ticker lookup).
    calls = {"n": 0}
    real_select = ccf.select_front_month_symbol

    def failing_select(product: str) -> str:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("Fubon CCF ticker lookup token expired")
        return real_select(product)

    ccf.select_front_month_symbol = failing_select
    terminal = io.StringIO()

    # The loop must survive the resolution failures rather than crash.
    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T02:45:00+08:00",
                "2026-06-18T02:45:30+08:00",
                "2026-06-18T02:45:59+08:00",
                "2026-06-18T02:46:01+08:00",
                "2026-06-18T02:46:59+08:00",
                "2026-06-18T02:47:01+08:00",
                "2026-06-18T02:47:02+08:00",
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
            or ts("2026-06-18T02:45:00+08:00"),
            state,
            IndicatorEngine(window=config.strategy.zscore_window),
        )
        store.commit()
    finally:
        store.close()


def open_position_state(*, state: StrategyState) -> StrategyRuntimeState:
    entry_time = ts("2026-06-18T02:40:00+08:00")
    return StrategyRuntimeState(
        state=state,
        position_direction=Direction.SHORT_UMC_LONG_CCF,
        exit_signal_idx=0 if state == StrategyState.EXIT_PENDING else -1,
        exit_signal_time=ts("2026-06-18T02:45:00+08:00")
        if state == StrategyState.EXIT_PENDING
        else None,
        exit_signal_zscore=-0.1 if state == StrategyState.EXIT_PENDING else None,
        entry_umc=100.0,
        entry_ccf=100.0,
        entry_zscore=2.2,
        umc_units=-10_000.0,
        ccf_units=10_000.0,
        ccf_contracts=100,
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
            "direction": Direction.SHORT_UMC_LONG_CCF.value,
            "entry_umc_twd_fair": 100.0,
            "entry_ccf_close": 100.0,
            "umc_units": -10_000.0,
            "ccf_units": 10_000.0,
            "ccf_contracts": 100,
            "raw_ccf_contracts": 100.0,
            "leg_notional_twd": 1_000_000.0,
            "actual_leg_notional_twd": 1_000_000.0,
            "ccf_contract_multiplier": 100.0,
            "entry_umc_fee_twd": 500.0,
            "entry_ccf_fee_twd": 500.0,
            "entry_ccf_tax_twd": 2.0,
            "entry_fee_twd": 1002.0,
            "ccf_symbol": "CCFG6",
            "ccf_expiry": "2026-07-15",
            "contract_policy_state": "active",
        },
        trading_ccf_symbol="CCFG6",
        trading_ccf_expiry="2026-07-15",
        eligible_active_ccf_symbol="CCFG6",
        eligible_active_ccf_expiry="2026-07-15",
        last_warmup_symbol="CCFG6",
        contract_policy_state="active",
    )


def test_live_dry_run_exit_pending_records_exit_intent(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_strategy_state(config, open_position_state(state=StrategyState.EXIT_PENDING))
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T02:45:30+08:00",
            "2026-06-18T02:45:59+08:00",
            "2026-06-18T02:46:01+08:00",
        ]
    )

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T02:45:00+08:00",
                "2026-06-18T02:45:30+08:00",
                "2026-06-18T02:45:59+08:00",
                "2026-06-18T02:46:01+08:00",
                "2026-06-18T02:46:02+08:00",
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
        assert sides[BrokerName.IBKR_UMC.value] == OrderSide.BUY.value
        assert sides[BrokerName.FUBON_CCF.value] == OrderSide.SELL.value
        assert plan["ccf_symbol"] == "CCFG6"
        assert plan["ccf_expiry"] == "2026-07-15"
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
    state.trading_ccf_expiry = "2026-06-19"
    state.open_trade["ccf_expiry"] = "2026-06-19"
    seed_strategy_state(config, state)
    # The rollover deadline is the pair session's 04:00 close less the 5-minute
    # grace, so the bar that must trigger it is 03:55 -- not the old 13:35, which
    # sat in the TAIFEX day session where this pair does not trade at all.
    force_ccf_rows = rows(
        [
            ("2026-06-18T03:52:00+08:00", 100.0),
            ("2026-06-18T03:53:00+08:00", 100.0),
            ("2026-06-18T03:54:00+08:00", 100.0),
        ]
    )
    force_umc_rows = rows(
        [
            ("2026-06-18T03:52:00+08:00", 20.0),
            ("2026-06-18T03:53:00+08:00", 20.0),
            ("2026-06-18T03:54:00+08:00", 20.0),
        ]
    )
    force_usd_rows = rows(
        [
            ("2026-06-18T03:52:00+08:00", 25.0),
            ("2026-06-18T03:53:00+08:00", 25.0),
            ("2026-06-18T03:54:00+08:00", 25.0),
        ]
    )
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-18T03:55:30+08:00",
            "2026-06-18T03:55:59+08:00",
            "2026-06-18T03:56:01+08:00",
        ],
        ccf_rows=force_ccf_rows,
        umc_rows=force_umc_rows,
        usd_rows=force_usd_rows,
    )

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-18T03:55:00+08:00",
                "2026-06-18T03:55:30+08:00",
                "2026-06-18T03:55:59+08:00",
                "2026-06-18T03:56:01+08:00",
                "2026-06-18T03:56:02+08:00",
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


def test_live_runtime_auto_warmup_builds_seed_on_empty_store(tmp_path) -> None:
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 101.0),
                ("2026-06-18T02:44:00+08:00", 102.0),
            ]
        )
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.5),
                ("2026-06-18T02:44:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        )
    )
    terminal_output = io.StringIO()

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=lambda: ts("2026-06-18T02:45:00+08:00"),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(reset_store=True, max_iterations=0)

    assert result.iterations == 0
    assert result.ccf_symbol == "CCFG6"
    assert ccf.fetch_1m_calls
    output = terminal_output.getvalue()
    assert "EVENT startup store_ready" in output
    assert "EVENT startup init_umc" in output
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


def test_live_runtime_resume_rebuilds_existing_seed_from_fresh_sources(tmp_path) -> None:
    config = small_live_config(tmp_path)
    warmup_ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 101.0),
                ("2026-06-18T02:44:00+08:00", 102.0),
            ]
        )
    )
    warmup_umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.5),
                ("2026-06-18T02:44:00+08:00", 21.0),
            ]
        )
    )
    warmup_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        )
    )
    WarmupRunner(
        config,
        ccf_provider=warmup_ccf,
        ccf_fallback_provider=None,
        umc_provider=warmup_umc,
        usd_twd_provider=warmup_usd,
    ).run(reset_store=True, end=ts("2026-06-18T02:45:00+08:00"))

    fresh_rows = rows(
        [
            ("2026-06-18T02:45:00+08:00", 110.0),
            ("2026-06-18T02:46:00+08:00", 111.0),
            ("2026-06-18T02:47:00+08:00", 112.0),
        ]
    )
    live_ccf = FakeCcfProvider(fresh_rows)
    live_umc = FakeOhlcvProvider(fresh_rows)
    live_usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )
    terminal_output = io.StringIO()

    LiveDryRunRunner(
        config,
        ccf_provider=live_ccf,
        umc_provider=live_umc,
        usd_twd_provider=live_usd,
        clock=lambda: ts("2026-06-18T02:48:00+08:00"),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(terminal_output, color=False),
    ).run(resume=True, max_iterations=0)

    assert live_ccf.fetch_1m_calls
    assert live_umc.fetch_ohlcv_calls
    assert live_usd.fetch_ohlcv_calls
    assert "EVENT warmup_auto start" in terminal_output.getvalue()
    assert "EVENT warmup_auto done_3" in terminal_output.getvalue()

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        refreshed = store.load_indicator_seed_bars(3, ccf_symbol="CCFG6")
        assert [bar.timestamp for bar in refreshed] == [
            ts("2026-06-18T02:45:00+08:00"),
            ts("2026-06-18T02:46:00+08:00"),
            ts("2026-06-18T02:47:00+08:00"),
        ]
        # Fresh resume warmup replaces only the seed snapshot.  Downtime is
        # never backfilled into the strategy's formal live-bar history.
        assert count_table(store, "bars") == 0
    finally:
        store.close()


def test_live_runtime_non_resume_refreshes_existing_seed_by_default(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    fresh_ccf = rows(
        [
            ("2026-06-18T02:45:00+08:00", 110.0),
            ("2026-06-18T02:46:00+08:00", 111.0),
            ("2026-06-18T02:47:00+08:00", 112.0),
        ]
    )
    fresh_umc = rows(
        [
            ("2026-06-18T02:45:00+08:00", 20.0),
            ("2026-06-18T02:46:00+08:00", 20.0),
            ("2026-06-18T02:47:00+08:00", 20.0),
        ]
    )
    fresh_usd = rows(
        [
            ("2026-06-18T02:45:00+08:00", 30.0),
            ("2026-06-18T02:46:00+08:00", 30.0),
            ("2026-06-18T02:47:00+08:00", 30.0),
        ]
    )
    ccf = FakeCcfProvider(fresh_ccf)

    LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=FakeOhlcvProvider(fresh_umc),
        usd_twd_provider=FakeOhlcvProvider(fresh_usd),
        clock=lambda: ts("2026-06-18T02:48:00+08:00"),
        sleeper=lambda _: None,
    ).run(max_iterations=0)

    assert ccf.fetch_1m_calls
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        refreshed = store.load_indicator_seed_bars(3, ccf_symbol="CCFG6")
        assert [bar.timestamp for bar in refreshed] == [
            ts("2026-06-18T02:45:00+08:00"),
            ts("2026-06-18T02:46:00+08:00"),
            ts("2026-06-18T02:47:00+08:00"),
        ]
    finally:
        store.close()


def test_live_runtime_resume_refuses_cached_seed_when_refresh_fails(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    ccf = FakeCcfProvider(pd.DataFrame())
    umc = FakeOhlcvProvider(pd.DataFrame())
    usd = FakeOhlcvProvider(pd.DataFrame())

    with pytest.raises(RuntimeError):
        LiveDryRunRunner(
            config,
            ccf_provider=ccf,
            umc_provider=umc,
            usd_twd_provider=usd,
            clock=lambda: ts("2026-06-18T02:48:00+08:00"),
            sleeper=lambda _: None,
        ).run(resume=True, max_iterations=0)

    assert ccf.fetch_1m_calls
    assert umc.fetch_ohlcv_calls == []
    assert usd.fetch_ohlcv_calls == []


def test_live_runtime_resume_rejects_skip_warmup(tmp_path) -> None:
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)

    with pytest.raises(RuntimeError, match="requires a fresh warmup rebuild"):
        LiveDryRunRunner(
            config,
            ccf_provider=FakeCcfProvider(pd.DataFrame()),
            umc_provider=FakeOhlcvProvider(pd.DataFrame()),
            usd_twd_provider=FakeOhlcvProvider(pd.DataFrame()),
            clock=lambda: ts("2026-06-18T02:48:00+08:00"),
            sleeper=lambda _: None,
        ).run(resume=True, max_iterations=0, skip_warmup=True)


def test_live_runtime_skip_warmup_requires_existing_seed(tmp_path) -> None:
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 100.0),
                ("2026-06-18T02:43:00+08:00", 101.0),
                ("2026-06-18T02:44:00+08:00", 102.0),
            ]
        )
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 20.0),
                ("2026-06-18T02:43:00+08:00", 20.5),
                ("2026-06-18T02:44:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:42:00+08:00", 30.0),
                ("2026-06-18T02:43:00+08:00", 30.0),
                ("2026-06-18T02:44:00+08:00", 30.0),
            ]
        )
    )

    with pytest.raises(RuntimeError, match="Warmup seed is missing"):
        LiveDryRunRunner(
            config,
            ccf_provider=ccf,
            umc_provider=umc,
            usd_twd_provider=usd,
            clock=lambda: ts("2026-06-18T02:45:00+08:00"),
            sleeper=lambda _: None,
        ).run(reset_store=True, max_iterations=0, skip_warmup=True)

    assert ccf.fetch_1m_calls == []
    assert umc.fetch_ohlcv_calls == []
    assert usd.fetch_ohlcv_calls == []


def test_live_runtime_rejects_live_order_flag(tmp_path) -> None:
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
        LiveDryRunRunner(config).run(max_iterations=0)


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
    ccf = FakeCcfProvider(rows([("2026-06-18T02:45:00+08:00", 100.0)]))
    umc = FakeOhlcvProvider(rows([("2026-06-18T02:45:00+08:00", 20.0)]))
    usd = FakeOhlcvProvider(rows([("2026-06-18T02:45:00+08:00", 30.0)]))

    with pytest.raises(RuntimeError, match="allow_live_order"):
        WarmupRunner(
            config,
            ccf_provider=ccf,
            ccf_fallback_provider=None,
            umc_provider=umc,
            usd_twd_provider=usd,
        ).run(reset_store=True)

    assert ccf.select_calls == 0
    assert ccf.fetch_1m_calls == []
    assert umc.fetch_ohlcv_calls == []
    assert usd.fetch_ohlcv_calls == []


def test_warmup_runner_fixed_symbol_skips_front_month_selector_and_writes_seed_only(tmp_path) -> None:
    config = small_live_config(tmp_path)
    config = replace(config, live=replace(config.live, ccf_symbol="CCF202607"))
    ccf = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:46:00+08:00", 101.0),
                ("2026-06-18T02:47:00+08:00", 102.0),
            ]
        )
    )
    umc = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 20.0),
                ("2026-06-18T02:46:00+08:00", 20.5),
                ("2026-06-18T02:47:00+08:00", 21.0),
            ]
        )
    )
    usd = FakeOhlcvProvider(
        rows(
            [
                ("2026-06-18T02:45:00+08:00", 30.0),
                ("2026-06-18T02:46:00+08:00", 30.0),
                ("2026-06-18T02:47:00+08:00", 30.0),
            ]
        )
    )

    result = WarmupRunner(
        config,
        ccf_provider=ccf,
        ccf_fallback_provider=None,
        umc_provider=umc,
        usd_twd_provider=usd,
    ).run(reset_store=True, end=ts("2026-06-18T02:48:00+08:00"))

    assert result.bars_written == 3
    assert result.ccf_symbol == "CCFG6"
    assert ccf.select_calls == 0
    assert ccf.fetch_1m_calls[0][0] == "CCFG6"

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


def test_ccf_warmup_check_runner_uses_fubon_and_taifex_only(tmp_path) -> None:
    config = small_live_config(tmp_path)
    ccf = FakeCcfProvider(rows([("2026-06-18T02:47:00+08:00", 103.0)]))
    taifex = FakeCcfProvider(
        rows(
            [
                ("2026-06-18T02:44:00+08:00", 99.0),
                ("2026-06-18T02:45:00+08:00", 100.0),
                ("2026-06-18T02:46:00+08:00", 101.0),
            ]
        )
    )

    result = CcfWarmupCheckRunner(
        config,
        ccf_provider=ccf,
        taifex_provider=taifex,
    ).run(output_csv="", end=ts("2026-06-18T02:48:00+08:00"))

    assert result.ccf_symbol == "CCFG6"
    assert len(result.report.frame) == 3
    assert result.report.null_count == 0
    assert result.report.source_rows == {"taifex": 3, "fubon": 1}
    assert result.output_csv is None


def test_ccf_warmup_check_refuses_wholly_missing_current_session(tmp_path) -> None:
    config = small_live_config(tmp_path)
    day_only = rows(
        [
            ("2026-06-18T03:43:00+08:00", 100.0),
            ("2026-06-18T03:44:00+08:00", 101.0),
            ("2026-06-18T03:45:00+08:00", 102.0),
        ]
    )

    with pytest.raises(RuntimeError, match="expected warmup window"):
        CcfWarmupCheckRunner(
            config,
            ccf_provider=FakeCcfProvider(day_only),
            taifex_provider=FakeCcfProvider(day_only),
        ).run(output_csv="", end=ts("2026-06-18T19:42:00+08:00"))


def test_contract_switch_cancels_entry_pending_state(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(
        state=StrategyState.ENTRY_PENDING,
        candidate_direction=Direction.SHORT_UMC_LONG_CCF,
        candidate_idx=10,
        candidate_time=ts("2026-07-09T08:45:00+08:00"),
        candidate_zscore=2.1,
        trading_ccf_symbol="CCFG6",
    )
    contract = CcfContractResolution(
        symbol="CCFH6",
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
        missing_reason="stale_ccf",
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
    assert signal_block_reason == "stale_ccf"


def test_live_decision_uses_opposite_tradable_spread_for_exit(tmp_path) -> None:
    config = small_live_config(tmp_path)
    short_state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_UMC_LONG_CCF,
    )
    long_state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.LONG_UMC_SHORT_CCF,
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
        position_direction=Direction.SHORT_UMC_LONG_CCF,
        trading_ccf_symbol="CCFG6",
    )
    contract = CcfContractResolution(
        symbol="CCFH6",
        expiry="2026-08-19",
        policy_state="active",
    )

    assert not should_switch_contract_before_processing(state, contract)
    mark_pending_contract_switch_if_needed(state, contract)

    assert state.pending_symbol_switch
    assert state.contract_policy_state == "pending_symbol_switch"
    assert state.eligible_active_ccf_symbol == "CCFH6"


def test_contract_policy_force_exit_helper_uses_configured_deadline(tmp_path) -> None:
    config = small_live_config(tmp_path)
    state = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_UMC_LONG_CCF,
        trading_ccf_symbol="CCFG6",
        trading_ccf_expiry="2026-07-15",
    )

    # 03:55 = the pair session's 04:00 close less the 5-minute grace, not a
    # TAIFEX wall clock. See core/contract_policy.force_exit_deadline.
    assert not should_force_exit_for_contract_policy(
        config,
        state,
        ts("2026-07-14T03:54:00+08:00"),
    )
    assert should_force_exit_for_contract_policy(
        config,
        state,
        ts("2026-07-14T03:55:00+08:00"),
    )


def test_resolve_force_exit_reason_weekend_requires_open_position(tmp_path) -> None:
    config = small_live_config(tmp_path)
    # 03:58 on Saturday is inside the grace window before the pair's last
    # session close of the week (Friday's US session, closing 04:00 Taipei).
    weekend_bar = ts("2026-06-20T03:58:00+08:00")
    # Mid-session on an ordinary Wednesday-into-Thursday: in the pair's window,
    # nowhere near its close.
    weekday_bar = ts("2026-06-18T03:00:00+08:00")
    flat = StrategyRuntimeState(state=StrategyState.FLAT)
    open_far_expiry = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_UMC_LONG_CCF,
        trading_ccf_symbol="CCF202607",
        trading_ccf_expiry="2026-07-15",
    )

    # A flat strategy is never force-exited, even in the weekend grace window.
    assert resolve_force_exit_reason(config, flat, weekend_bar) is None
    # An open position outside the grace window is left alone.
    assert resolve_force_exit_reason(config, open_far_expiry, weekday_bar) is None
    # An open position at the Friday session end is flattened for the weekend.
    assert (
        resolve_force_exit_reason(config, open_far_expiry, weekend_bar)
        == "weekend_force_exit"
    )


def test_resolve_force_exit_reason_prefers_expiry_over_weekend(tmp_path) -> None:
    config = small_live_config(tmp_path)
    weekend_bar = ts("2026-06-20T03:58:00+08:00")
    # 2026-06-22 (Mon) expiry -> force-exit deadline is the pair session close on
    # the previous business day (Fri 2026-06-19 04:00) less the grace, i.e.
    # 03:55, already past by the weekend bar, so rollover wins.
    open_near_expiry = StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_UMC_LONG_CCF,
        trading_ccf_symbol="CCF202606",
        trading_ccf_expiry="2026-06-22",
    )

    assert (
        resolve_force_exit_reason(config, open_near_expiry, weekend_bar)
        == "rollover_force_exit"
    )


def test_live_dry_run_weekend_force_exit_flattens_before_weekend(tmp_path) -> None:
    config = small_live_config(tmp_path)
    # Far expiry: the contract-policy rollover must NOT fire, isolating the
    # weekend/session-end flatten as the only force-exit driver.
    seed_strategy_state(config, open_position_state(state=StrategyState.OPEN))
    # The pair's last session before the weekend is Friday 06-19's US session,
    # which closes at Taipei 06-20 04:00 -- an hour BEFORE the TAIFEX night
    # session it sits inside. The grace window is therefore 03:55-04:00, and the
    # bar that must trigger the flatten is 03:58.
    warmup_ccf = rows(
        [
            ("2026-06-20T03:55:00+08:00", 100.0),
            ("2026-06-20T03:56:00+08:00", 100.0),
            ("2026-06-20T03:57:00+08:00", 100.0),
        ]
    )
    warmup_umc = rows(
        [
            ("2026-06-20T03:55:00+08:00", 20.0),
            ("2026-06-20T03:56:00+08:00", 20.0),
            ("2026-06-20T03:57:00+08:00", 20.0),
        ]
    )
    warmup_usd = rows(
        [
            ("2026-06-20T03:55:00+08:00", 25.0),
            ("2026-06-20T03:56:00+08:00", 25.0),
            ("2026-06-20T03:57:00+08:00", 25.0),
        ]
    )
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-20T03:58:30+08:00",
            "2026-06-20T03:58:59+08:00",
            "2026-06-20T03:59:01+08:00",
        ],
        ccf_rows=warmup_ccf,
        umc_rows=warmup_umc,
        usd_rows=warmup_usd,
    )

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-20T03:58:00+08:00",
                "2026-06-20T03:58:30+08:00",
                "2026-06-20T03:58:59+08:00",
                "2026-06-20T03:59:01+08:00",
                "2026-06-20T03:59:02+08:00",
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
        assert plan["reason"] == "weekend_force_exit"
        event_types = [
            row["event_type"]
            for row in store.connection.execute(
                "SELECT event_type FROM events ORDER BY event_id"
            ).fetchall()
        ]
        assert "weekend_force_exit" in event_types
        assert "rollover_force_exit" not in event_types
        assert count_table(store, "trades") == 1
        state = store.load_resume_state()
        assert state is not None
        assert state.strategy.state == StrategyState.FLAT
    finally:
        store.close()


def test_a_book_the_stream_stopped_refreshing_is_not_served_as_a_quote() -> None:
    """REGRESSION 2026-08-15: a websocket the peer closed cleanly raises no
    error event, so the books cache was never cleared and `fetch_quote` handed
    back the same LiveQuote for three hours. CCF sat at 120.75 while every poll
    recorded it as current."""
    provider = FubonCcfMarketData(
        None,
        book_wait_timeout_seconds=0.0,
        book_stale_seconds=55.0,
    )
    provider.intraday = FakeFubonIntraday(
        {"REGULAR": {"data": []}, "AFTERHOURS": {"data": []}}
    )
    websocket = FakeFubonWebSocket()
    provider.websocket = websocket
    provider.ensure_books_subscription("CCFG6", after_hours=True)
    websocket.emit(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "CCFG6",
                "time": "2026-06-18T02:45:01+08:00",
                "bids": [{"price": 2409.0, "size": 3}],
                "asks": [{"price": 2411.0, "size": 4}],
            },
        }
    )

    # Fresh off the wire: served.
    assert provider._wait_for_book_quote("CCFG6") is not None

    # Same book, nothing new for longer than the budget: no longer a quote.
    provider._book_received_at["CCFG6"] -= 56.0
    assert provider._wait_for_book_quote("CCFG6") is None

    # And a new message revives it without needing a resubscribe.
    websocket.emit(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "CCFG6",
                "time": "2026-06-18T02:46:01+08:00",
                "bids": [{"price": 2412.0, "size": 3}],
                "asks": [{"price": 2414.0, "size": 4}],
            },
        }
    )
    revived = provider._wait_for_book_quote("CCFG6")
    assert revived is not None and revived.bid == 2412.0


def test_ccf_books_quote_carries_a_price_timestamp() -> None:
    """`price` is the book midpoint, so its clock IS the book clock. Leaving
    price_timestamp None read as 'this source has no price clock' to every
    gate that consults it."""
    parsed = parse_fubon_books_quote(
        {
            "event": "data",
            "channel": "books",
            "data": {
                "symbol": "CCFG6",
                "time": "2026-06-18T02:45:01+08:00",
                "bids": [{"price": 2409.0, "size": 3}],
                "asks": [{"price": 2411.0, "size": 4}],
            },
        }
    )

    assert parsed is not None
    assert parsed.price_timestamp == parsed.timestamp
    assert parsed.price_timestamp == ts("2026-06-18T02:45:01+08:00")


def test_forward_filled_ccf_is_refused_once_the_carry_outlives_its_budget() -> None:
    """REGRESSION 2026-08-15: `stale_seconds` decided whether THIS minute's CCF
    quote could be the close, and nothing bounded how long the previous one was
    carried when it could not. A dead feed produced a bar every minute from a
    price hours old."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        ccf_forward_fill_max_seconds=55.0,
        max_leg_timestamp_skew_seconds=30.0,
    )
    builder.last_ccf_close = 100.0
    # The last CCF quote anyone confirmed. The feed dies immediately after.
    builder.last_ccf_close_at = ts("2026-06-18T02:45:30+08:00")
    frozen_ccf = "2026-06-18T02:45:30+08:00"

    def minute_at(moment: str) -> LiveQuoteSet:
        """UMC and FX current for their minute; CCF stuck at 02:45:30."""
        return LiveQuoteSet(
            ccf=quote("ccf", frozen_ccf, 100.0),
            umc=quote("umc", moment, 20.0),
            usd_twd=quote("usd", moment, 30.0),
        )

    # update() finalizes the PREVIOUS minute, so each call closes the one before.
    builder.update(minute_at("2026-06-18T02:45:59+08:00"), ts("2026-06-18T02:45:59+08:00"))

    # Minute 02:45 closes at 02:46:00 -- the carry is 30s, inside the ceiling.
    # CCF is already past the 10s freshness budget, so this bar is forward
    # filled, which is the behaviour a thin minute is supposed to get.
    within = builder.update(
        minute_at("2026-06-18T02:46:59+08:00"), ts("2026-06-18T02:46:59+08:00")
    )
    assert within is not None
    assert within.skipped_reason is None
    assert within.bar is not None
    assert within.bar.ccf_close is None
    assert within.bar.ccf_close_filled == 100.0

    # Minute 02:46 closes at 02:47:00 -- the carry is now 90s. Refused rather
    # than built from a price nobody has confirmed for a minute and a half.
    beyond = builder.update(
        minute_at("2026-06-18T02:47:59+08:00"), ts("2026-06-18T02:47:59+08:00")
    )
    assert beyond is not None
    assert beyond.skipped_reason == "market_data_stale"
    assert beyond.payload["source"] == "ccf"
    assert beyond.payload["forward_filled"] is True
    assert beyond.payload["age_seconds"] == pytest.approx(90.0)


def test_a_fresh_ccf_quote_restarts_the_forward_fill_budget() -> None:
    """The ceiling must not accumulate across healthy minutes: a thin feed that
    prints once a minute is normal, and rejecting it would be the same class of
    mistake as the bug the ceiling exists to stop."""
    builder = LiveMinuteBarBuilder(
        stale_seconds=10.0,
        usd_twd_stale_seconds=600.0,
        ccf_forward_fill_max_seconds=55.0,
        max_leg_timestamp_skew_seconds=30.0,
    )
    builder.last_ccf_close = 100.0
    builder.last_ccf_close_at = ts("2026-06-18T02:45:00+08:00")

    base = ts("2026-06-18T02:45:59+08:00")
    for index in range(6):
        moment = base + timedelta(minutes=index)
        result = builder.update(
            LiveQuoteSet(
                ccf=LiveQuote(
                    source="ccf",
                    symbol="ccf",
                    timestamp=moment,
                    price_timestamp=moment,
                    price=100.0 + index,
                ),
                umc=quote("umc", moment.isoformat(), 20.0),
                usd_twd=quote("usd", moment.isoformat(), 30.0),
            ),
            moment,
        )
        if result is not None:
            assert result.skipped_reason is None


def test_a_failed_teardown_is_recorded_rather_than_swallowed() -> None:
    """REGRESSION 2026-08-15: both teardown steps swallowed their exception
    with a bare pass. When a disconnect fails the old SDK's threads outlive the
    object that owned them, and the only symptom is a socket nobody closes --
    tracing it took hours because nothing recorded the failure."""

    class RefusesToDisconnect:
        def __init__(self) -> None:
            self.listeners: dict[str, object] = {}

        def on(self, event: str, listener: object) -> None:
            self.listeners[event] = listener

        def disconnect(self) -> None:
            raise OSError("WinError 10054")

    provider = FubonCcfMarketData(None, book_wait_timeout_seconds=0.0)
    provider.websocket = RefusesToDisconnect()

    # Teardown must still complete: recording cannot become a new failure mode.
    provider.teardown_books_session()

    assert len(provider.last_cleanup_errors) == 1
    recorded = provider.last_cleanup_errors[0]
    assert recorded.startswith("websocket.disconnect: OSError:")
    assert "10054" in recorded


def test_cleanup_error_history_is_bounded() -> None:
    """A teardown that fails every session must not grow without limit."""
    provider = FubonCcfMarketData(None, book_wait_timeout_seconds=0.0)
    for index in range(25):
        provider._record_cleanup_error("websocket.disconnect", OSError(str(index)))

    assert len(provider.last_cleanup_errors) == 8
    assert provider.last_cleanup_errors[-1].endswith("24")


def test_completing_a_rollover_rebuilds_warmup_on_the_new_contract(tmp_path) -> None:
    """REGRESSION 2026-08-17: switch_to_contract used load_or_build_live_indicator
    without importing it, and raised NameError the first time a rollover
    completed live -- after the position had already closed.

    It survived because nothing called this function. The three contract-switch
    tests all cover the DECISION to roll (cancel entry-pending, mark
    pending_symbol_switch); none reached the switch itself, which only runs once
    a pending rollover goes flat.

    The import has to stay inside the function: warmup imports
    resolve_ccf_contract from contracts, so a module-level import closes the
    cycle and neither module loads. That makes "does this name resolve at call
    time" a real question, and this is the test that asks it.
    """
    config = small_live_config(tmp_path)
    # Same window the auto-warmup test uses: a mid-session stretch with no
    # weekend behind it, so the builder's lookback lands on real bars.
    bars = [
        ("2026-06-18T02:42:00+08:00", 100.0),
        ("2026-06-18T02:43:00+08:00", 101.0),
        ("2026-06-18T02:44:00+08:00", 102.0),
    ]
    ccf = FakeCcfProvider(rows(bars))
    umc = FakeOhlcvProvider(rows([(t, 20.0 + i) for i, (t, _) in enumerate(bars)]))
    usd = FakeOhlcvProvider(rows([(t, 30.0) for t, _ in bars]))

    state = StrategyRuntimeState(
        state=StrategyState.FLAT,
        trading_ccf_symbol="CCFH6",
        eligible_active_ccf_symbol="CCFI6",
        pending_symbol_switch=True,
        contract_policy_state="pending_symbol_switch",
    )
    contract = CcfContractResolution(
        symbol="CCFI6",
        expiry="2026-09-16",
        policy_state="active",
    )

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        symbol, expiry, indicator, seed_bars = switch_to_contract(
            store,
            config,
            state,
            contract,
            ccf_provider=ccf,
            umc_provider=umc,
            usd_twd_provider=usd,
            end=ts("2026-06-18T02:45:00+08:00"),
        )
    finally:
        store.close()

    # The switch completed rather than raising, and moved the state onto CCFI6.
    assert symbol == "CCFI6"
    assert expiry == "2026-09-16"
    assert state.trading_ccf_symbol == "CCFI6"
    assert state.eligible_active_ccf_symbol == "CCFI6"
    assert state.pending_symbol_switch is False
    assert state.contract_policy_state == "active"
    assert state.last_warmup_symbol == "CCFI6"

    # Warmup was rebuilt on the NEW contract, not carried over from the old one.
    assert indicator is not None
    assert len(seed_bars) == len(bars)
    assert ccf.fetch_1m_calls, "the new contract's history was never fetched"
    assert ccf.fetch_1m_calls[0][0] == "CCFI6"


def test_a_completed_rollover_retargets_the_order_path_too(tmp_path) -> None:
    """REGRESSION 2026-08-18: the rollover moved the strategy state and the
    books subscription to CCFI6 and left the Fubon order adapter on CCFH6.
    The next entry signal was rejected with "Fubon leg symbol CCFI6 does not
    match CCFH6" and the strategy paused -- no order was ever submitted, which
    is the adapter's check doing its job, but the rollover was only half done.

    Duck-typed on purpose: a simulated adapter has no contract to move.
    """
    retargeted: list[str] = []

    class FakeFubonAdapter:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def retarget_symbol(self, symbol: str) -> bool:
            if symbol == self.symbol:
                return False
            self.symbol = symbol
            retargeted.append(symbol)
            return True

    class AdapterWithoutRetarget:
        """Stands in for the simulated adapter; must simply be skipped."""

    adapter = FakeFubonAdapter("CCFH6")
    handler = LiveExecuteModeHandler(
        small_live_config(tmp_path),
        fubon_adapter=adapter,
        # The same object is returned as both adapter and read-only broker by
        # build_live_execution_brokers, so it must not be retargeted twice.
        readonly_brokers=(adapter, AdapterWithoutRetarget()),
    )
    reporter = LiveTerminalReporter(io.StringIO(), color=False)

    handler.on_contract_switched(
        ccf_symbol="CCFI6",
        reporter=reporter,
        timestamp=ts("2026-08-18T21:30:00+08:00"),
    )

    assert adapter.symbol == "CCFI6"
    assert retargeted == ["CCFI6"], "the shared adapter was retargeted more than once"

    # Idempotent: the rollover check runs every bar.
    handler.on_contract_switched(
        ccf_symbol="CCFI6",
        reporter=reporter,
        timestamp=ts("2026-08-18T21:31:00+08:00"),
    )
    assert retargeted == ["CCFI6"]


def test_a_dry_run_rollover_does_not_need_a_contract_to_retarget(tmp_path) -> None:
    """The base hook is a no-op, and must stay callable: the runtime calls it
    for every mode, and a dry run holds no broker session bound to a contract."""
    handler = DryRunLiveModeHandler(small_live_config(tmp_path))

    handler.on_contract_switched(
        ccf_symbol="CCFI6",
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
        timestamp=ts("2026-08-18T21:30:00+08:00"),
    )


def test_a_flat_rollover_retargets_the_order_path_too(tmp_path) -> None:
    """REGRESSION: the retarget added in 6ccf109 went onto the after-flat path
    only. The FLAT / ENTRY_PENDING path is the one an ordinary roll takes --
    pending_symbol_switch is set only while a position is open -- so the common
    case still left the Fubon adapter bound to the expired contract and paused
    on the next entry, exactly as on 2026-08-18.

    This asserts the call exists on BOTH paths rather than the behaviour of one,
    because the defect was an omission at a call site, not a wrong branch.
    """
    import inspect

    from lux_trader.runtime.live import engine as live_engine

    for name in (
        "_switch_contract_before_processing",
        "_complete_contract_switch_after_flat",
    ):
        source = inspect.getsource(getattr(live_engine.LiveRuntime, name))
        assert "switch_to_contract(" in source, f"{name} no longer switches contract"
        assert "on_contract_switched(" in source, (
            f"{name} switches the contract without retargeting the order path; "
            "the adapter keeps the expired symbol and the next entry is refused"
        )


def test_reconnect_umc_provider_if_supported_mirrors_ccf_under_its_own_label() -> None:
    """The UMC socket needs this for a different reason than CCF's token: it
    sits idle for the 17.5 hours between sessions, through IBKR's nightly
    reset, and a link the peer closed still reports itself connected."""
    from lux_trader.runtime.live.contracts import reconnect_umc_provider_if_supported

    class _ReconProvider:
        def __init__(self, exc: Exception | None = None) -> None:
            self.calls = 0
            self.exc = exc

        def reconnect(self) -> None:
            self.calls += 1
            if self.exc is not None:
                raise self.exc

    when = ts("2026-06-18T02:45:00+08:00")

    out = io.StringIO()
    provider = _ReconProvider()
    reconnect_umc_provider_if_supported(
        provider, LiveTerminalReporter(out, color=False), when
    )
    assert provider.calls == 1
    # Its own label, so an operator can tell which venue reconnected.
    assert "umc_quote" in out.getvalue()
    assert "reconnect_login" in out.getvalue()

    out_fail = io.StringIO()
    raising = _ReconProvider(exc=RuntimeError("socket boom"))
    reconnect_umc_provider_if_supported(
        raising, LiveTerminalReporter(out_fail, color=False), when
    )
    assert raising.calls == 1
    # A failed reconnect is reported and swallowed: the per-quote path still
    # gets its chance, and trading is never stopped by it.
    assert "reconnect_failed" in out_fail.getvalue()

    reconnect_umc_provider_if_supported(
        object(), LiveTerminalReporter(io.StringIO(), color=False), when
    )


def test_live_runtime_reconnects_the_umc_socket_on_session_open(tmp_path) -> None:
    """The CCF leg has always been re-established at the open; the UMC leg was
    left to discover its dead socket by failing a quote. Same transition, both
    venues."""
    config = small_live_config(tmp_path)
    seed_warmup_bars(config)
    ccf, umc, usd = dry_run_quote_providers(
        [
            "2026-06-23T03:58:01+08:00",
            "2026-06-23T21:30:00+08:00",
        ]
    )
    umc_reconnects: list[int] = []
    umc.reconnect = lambda: umc_reconnects.append(1)

    result = LiveDryRunRunner(
        config,
        ccf_provider=ccf,
        umc_provider=umc,
        usd_twd_provider=usd,
        clock=dry_run_clock(
            [
                "2026-06-23T03:58:00+08:00",
                "2026-06-23T03:58:01+08:00",
                "2026-06-23T04:01:00+08:00",
                "2026-06-23T21:30:00+08:00",
                "2026-06-23T21:30:01+08:00",
            ]
        ),
        sleeper=lambda _: None,
    ).run(max_iterations=3, skip_warmup=True)

    assert result.iterations == 3
    assert ccf.restart_books_calls == ["CCFG6"]
    # Once, on the reopen -- not on every trading iteration.
    assert umc_reconnects == [1]
