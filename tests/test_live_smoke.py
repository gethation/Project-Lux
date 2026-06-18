from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lux_trader.config import AppConfig, load_config
from lux_trader.live_market_data import (
    CcxtTickerMarketData,
    FubonQffMarketData,
    TAIPEI_TZ,
    floor_minute,
)
from lux_trader.live_runner import QffWarmupCheckRunner, WarmupRunner, resolve_qff_contract


SMOKE_CONFIG = Path("config.live.smoke.local.toml")

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


def test_live_marketdata_providers_fetch_quotes_and_qff_candles() -> None:
    config = load_smoke_config()
    qff = FubonQffMarketData(config.live.fubon_env_path)
    try:
        contract = resolve_qff_contract(
            config,
            qff,
            now=datetime.now(TAIPEI_TZ),
        )
        if config.live.qff_symbol.lower() == "auto" and config.contract_policy.enabled:
            assert contract.expiry is not None
            assert contract.policy_state == "active"

        qff_quote = qff.fetch_quote(contract.symbol)
        assert qff_quote.price > 0

        end = floor_minute(datetime.now(TAIPEI_TZ)) - timedelta(minutes=1)
        start = end - timedelta(days=7)
        qff_candles = qff.fetch_1m(contract.symbol, start, end)
        assert not qff_candles.empty
        assert qff_candles["close"].notna().all()
    finally:
        qff.close()

    binance_quote = CcxtTickerMarketData("binanceusdm").fetch_quote(
        config.live.binance_symbol
    )
    bitopro_quote = CcxtTickerMarketData("bitopro").fetch_quote(
        config.live.bitopro_symbol
    )
    assert binance_quote.price > 0
    assert bitopro_quote.price > 0


def test_qff_warmup_check_smoke_uses_fubon_and_taifex_network() -> None:
    config = load_smoke_config()
    result = QffWarmupCheckRunner(config).run(output_csv="")

    assert len(result.report.frame) == config.live.warmup_minutes
    assert result.report.null_count == 0
    assert result.report.source_rows["taifex"] > 0
    assert result.report.source_rows["fubon"] > 0
    assert result.output_csv is None


def test_warmup_live_smoke_writes_1440_seed_bars_only() -> None:
    config = load_smoke_config()
    result = WarmupRunner(config).run(reset_store=True)

    assert result.bars_written == config.live.warmup_minutes
    assert result.bars_written == 1440

    connection = sqlite3.connect(config.store_path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("warmup_bars", "bars", "orders", "fills", "trades")
        }
        assert counts == {
            "warmup_bars": 1440,
            "bars": 0,
            "orders": 0,
            "fills": 0,
            "trades": 0,
        }
        null_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM warmup_bars
            WHERE qff_close_filled IS NULL
               OR tsm_twd_fair IS NULL
               OR spread IS NULL
            """
        ).fetchone()[0]
        assert null_count == 0
    finally:
        connection.close()
