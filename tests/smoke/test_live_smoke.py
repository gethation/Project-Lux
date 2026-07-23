from __future__ import annotations

import io
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lux_trader.config import AppConfig, load_config
from lux_trader.core.time import TAIPEI_TZ
from lux_trader.integrations.binance.market_data import BinanceMarketData
from lux_trader.integrations.bitopro.market_data import BitoProMarketData
from lux_trader.integrations.fubon.market_data import FubonTwLegMarketData
from lux_trader.market_data import floor_minute
from lux_trader.runtime.live import (
    LiveDryRunRunner,
    TwLegWarmupCheckRunner,
    WarmupRunner,
    resolve_tw_leg_contract,
)
from lux_trader.terminal_ui import LiveTerminalReporter


SMOKE_CONFIG = Path("configs/config.live.smoke.local.toml")

pytestmark = pytest.mark.live_marketdata


def load_smoke_config() -> AppConfig:
    if os.getenv("LUX_LIVE_MARKETDATA", "").strip() != "1":
        pytest.skip("Set LUX_LIVE_MARKETDATA=1 to run real market-data smoke tests")
    if not SMOKE_CONFIG.exists():
        pytest.skip(f"Smoke config does not exist: {SMOKE_CONFIG}")
    config = load_config(SMOKE_CONFIG)
    if config.safety.allow_live_order:
        pytest.fail("Smoke config must keep allow_live_order=false")
    return config


def startup_smoke_config() -> AppConfig:
    config = load_smoke_config()
    return replace(
        config,
        store_path=config.store_path.parent / "live_startup_smoke.sqlite3",
    )


def remove_sqlite_family(path: Path) -> None:
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if target.exists():
            target.unlink()


def test_live_marketdata_providers_fetch_quotes_and_tw_leg_candles() -> None:
    config = load_smoke_config()
    tw_leg = FubonTwLegMarketData(config.live.fubon_env_path)
    try:
        contract = resolve_tw_leg_contract(
            config,
            tw_leg,
            now=datetime.now(TAIPEI_TZ),
        )
        if config.live.tw_leg_symbol.lower() == "auto" and config.contract_policy.enabled:
            assert contract.expiry is not None
            assert contract.policy_state == "active"

        tw_leg_quote = tw_leg.fetch_quote(contract.symbol)
        assert tw_leg_quote.price > 0
        if tw_leg_quote.bid is not None or tw_leg_quote.ask is not None:
            assert tw_leg_quote.bid is not None
            assert tw_leg_quote.ask is not None
            assert tw_leg_quote.bid > 0
            assert tw_leg_quote.ask > 0

        end = floor_minute(datetime.now(TAIPEI_TZ)) - timedelta(minutes=1)
        start = end - timedelta(days=7)
        tw_leg_candles = tw_leg.fetch_1m(contract.symbol, start, end)
        assert not tw_leg_candles.empty
        assert tw_leg_candles["close"].notna().all()
    finally:
        tw_leg.close()

    binance_quote = BinanceMarketData().fetch_quote(
        config.live.binance_symbol
    )
    bitopro_quote = BitoProMarketData().fetch_quote(
        config.live.bitopro_symbol
    )
    assert binance_quote.price > 0
    assert binance_quote.bid is not None
    assert binance_quote.ask is not None
    assert binance_quote.bid > 0
    assert binance_quote.ask > 0
    assert bitopro_quote.price > 0
    assert bitopro_quote.bid is not None
    assert bitopro_quote.ask is not None
    assert bitopro_quote.bid > 0
    assert bitopro_quote.ask > 0


def test_tw_leg_warmup_check_smoke_uses_fubon_and_taifex_network() -> None:
    config = load_smoke_config()
    result = TwLegWarmupCheckRunner(config).run(output_csv="")

    assert len(result.report.frame) == config.live.warmup_minutes
    assert result.report.null_count == 0
    assert result.report.source_rows["taifex"] > 0
    assert result.report.source_rows["fubon"] > 0
    assert result.output_csv is None


def test_warmup_live_smoke_writes_seed_bars_only() -> None:
    config = load_smoke_config()
    result = WarmupRunner(config).run(reset_store=True)

    assert result.bars_written == config.live.warmup_minutes

    connection = sqlite3.connect(config.store_path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("warmup_bars", "bars", "orders", "fills", "trades")
        }
        assert counts == {
            "warmup_bars": config.live.warmup_minutes,
            "bars": 0,
            "orders": 0,
            "fills": 0,
            "trades": 0,
        }
        null_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM warmup_bars
            WHERE tw_leg_close_filled IS NULL
               OR us_leg_twd_fair IS NULL
               OR spread IS NULL
            """
        ).fetchone()[0]
        assert null_count == 0
    finally:
        connection.close()


def test_live_startup_smoke_auto_warmup_and_resume() -> None:
    config = startup_smoke_config()
    remove_sqlite_family(config.store_path)

    first_output = io.StringIO()
    first_result = LiveDryRunRunner(
        config,
        reporter=LiveTerminalReporter(first_output, color=False),
    ).run(reset_store=True, max_iterations=130)

    first_text = first_output.getvalue()
    assert first_result.iterations == 130
    assert "EVENT warmup_auto start" in first_text
    assert f"EVENT warmup_auto done_{config.live.warmup_minutes}" in first_text
    assert first_text.count("LIVE") > 1

    connection = sqlite3.connect(config.store_path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "warmup_bars",
                "bars",
                "orders",
                "fills",
                "trades",
                "market_ticks",
                "live_runs",
            )
        }
        assert counts["warmup_bars"] == config.live.warmup_minutes
        assert counts["market_ticks"] > 0
        assert counts["live_runs"] == 1
        assert counts["orders"] == counts["fills"]
        source_counts = {
            source: count
            for source, count in connection.execute(
                "SELECT source, COUNT(*) FROM market_ticks GROUP BY source"
            ).fetchall()
        }
        assert {"fubon_tw_leg", "binanceusdm", "bitopro"}.issubset(source_counts)
        book_counts = {
            source: count
            for source, count in connection.execute(
                """
                SELECT source, COUNT(*)
                FROM market_ticks
                WHERE bid IS NOT NULL AND ask IS NOT NULL
                GROUP BY source
                """
            ).fetchall()
        }
        assert book_counts.get("binanceusdm", 0) > 0
        assert book_counts.get("bitopro", 0) > 0
        null_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM warmup_bars
            WHERE tw_leg_close_filled IS NULL
               OR us_leg_twd_fair IS NULL
               OR spread IS NULL
               OR tw_leg_symbol IS NULL
            """
        ).fetchone()[0]
        assert null_count == 0
        selected_symbols = connection.execute(
            "SELECT DISTINCT tw_leg_symbol FROM warmup_bars"
        ).fetchall()
        assert selected_symbols == [(first_result.tw_leg_symbol,)]
        skipped_events = connection.execute(
            """
            SELECT COUNT(*)
            FROM events
            WHERE event_type IN (
                'market_data_stale',
                'leg_timestamp_skew',
                'missing_required_quote',
                'missing_tw_leg_forward_fill'
            )
            """
        ).fetchone()[0]
        assert counts["bars"] >= 1 or skipped_events >= 1
    finally:
        connection.close()

    second_output = io.StringIO()
    second_result = LiveDryRunRunner(
        config,
        reporter=LiveTerminalReporter(second_output, color=False),
    ).run(resume=True, max_iterations=70)

    assert second_result.iterations == 70
    assert "warmup_auto" not in second_output.getvalue()

    connection = sqlite3.connect(config.store_path)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM warmup_bars").fetchone()[0]
            == config.live.warmup_minutes
        )
        assert connection.execute("SELECT COUNT(*) FROM live_runs").fetchone()[0] == 2
        duplicate_bars = connection.execute(
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
    finally:
        connection.close()
