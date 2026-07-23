from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lux_trader.config import (
    AppConfig,
    BrokerReconciliationConfig,
    ContractPolicyConfig,
    FeeConfig,
    FxConfig,
    LiveExecutionConfig,
    LiveExecutionSmokeConfig,
    LiveMarketDataConfig,
    PairConfig,
    PairDataConfig,
    SafetyConfig,
    StrategyConfig,
    SizingConfig,
    TradingCalendarConfig,
    TwLegConfig,
    UsLegConfig,
)


# Frozen snapshot of the PoC reference replay inputs, committed under
# tests/fixtures/replay/. The replay acceptance test must be deterministic and
# self-contained: the live PoC working directory rebuilds tw_leg1_1m.csv from
# TAIFEX tick history, which only retains ~30 trading days, so the original
# reference dataset (and its 265,481 net PnL) ages out and cannot be rebuilt.
# These fixtures decouple the test from that mutable upstream. The OHLCV files
# are trimmed to the [timestamp, open] columns the replay actually reads.
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"
POC_CSV = _FIXTURE_DIR / "spread_zscore_w500.csv"
POC_QFF_OHLCV = _FIXTURE_DIR / "qff1_1m_open.csv"
POC_TSM_OHLCV = _FIXTURE_DIR / "binance_tsm_open.csv"
POC_USDTTWD_OHLCV = _FIXTURE_DIR / "bitopro_usdttwd_open.csv"


@pytest.fixture
def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        entry_z=2.0,
        exit_z=1.0,
        leg_notional_twd=1_000_000.0,
        initial_capital_twd=2_000_000.0,
        max_entry_delay_minutes=15,
        zscore_window=500,
    )


@pytest.fixture
def fee_config() -> FeeConfig:
    return FeeConfig(
        us_leg_fee_bps=5.0,
        tw_leg_fee_per_contract_twd=5.0,
        tw_leg_tax_rate=0.00002,
        tw_leg_contract_multiplier=100.0,
        us_leg_contract_multiplier=5.0,
    )


def make_app_config(tmp_path: Path, validate_expected_zscore: bool = True) -> AppConfig:
    config = AppConfig(
        input_csv=POC_CSV,
        store_path=tmp_path / "project_lux.sqlite3",
        tw_leg_ohlcv_csv=POC_QFF_OHLCV,
        us_leg_ohlcv_csv=POC_TSM_OHLCV,
        usdttwd_ohlcv_csv=POC_USDTTWD_OHLCV,
        strategy=StrategyConfig(
            entry_z=2.0,
            exit_z=1.0,
            leg_notional_twd=1_000_000.0,
            initial_capital_twd=2_000_000.0,
            max_entry_delay_minutes=15,
            zscore_window=500,
        ),
        fees=FeeConfig(
            us_leg_fee_bps=5.0,
            tw_leg_fee_per_contract_twd=5.0,
            tw_leg_tax_rate=0.00002,
            tw_leg_contract_multiplier=100.0,
            us_leg_contract_multiplier=5.0,
        ),
        safety=SafetyConfig(
            allow_live_order=False,
            validate_expected_zscore=validate_expected_zscore,
            expected_zscore_tolerance=1e-7,
        ),
        contract_policy=ContractPolicyConfig(
            enabled=True,
            min_business_days_to_expiry=5,
            force_exit_business_days_before_expiry=1,
            force_exit_time="13:35",
            holidays=(),
        ),
        trading_calendar=TradingCalendarConfig(closed_dates=()),
        live=LiveMarketDataConfig(
            polling_seconds=1.0,
            minute_finalize_delay_seconds=1.0,
            stale_seconds=10.0,
            tw_leg_book_stale_seconds=55.0,
            sync_windows_time_on_startup=True,
            clock_skew_fail_seconds=60.0,
            windows_time_sync_timeout_seconds=15.0,
            max_leg_timestamp_skew_seconds=10.0,
            warmup_minutes=500,
            tw_leg_product="QFF",
            tw_leg_symbol="auto",
            binance_symbol="TSM/USDT:USDT",
            bitopro_symbol="USDT/TWD",
            fubon_env_path=None,
            taifex_tw_leg_1m_csv=None,
            taifex_use_network=False,
            taifex_cache_dir=tmp_path / "taifex_cache",
        ),
        broker_reconciliation=BrokerReconciliationConfig(
            enabled=False,
            fail_on_mismatch=False,
            us_leg_units_tolerance=1e-6,
            tw_leg_contract_tolerance=0,
        ),
        live_execution=LiveExecutionConfig(
            enabled=False,
            require_readonly_reconciliation=True,
            max_plan_age_seconds=120,
            tw_leg_first=True,
        ),
        live_execution_smoke=LiveExecutionSmokeConfig(
            enabled=False,
            fubon_symbol="TMFG6",
            fubon_lots=1,
            binance_symbol="TSM/USDT:USDT",
            us_leg_units=0.1,
            tw_leg_expiry="202607",
        ),
    )
    pair = PairConfig(
        id="qff_tsm",
        label="QFF/TSM",
        tw_leg=TwLegConfig(
            display="QFF",
            venue="fubon",
            product="QFF",
            symbol="auto",
            contract_multiplier=100.0,
        ),
        us_leg=UsLegConfig(
            display="TSM",
            venue="binance",
            symbol="TSM/USDT:USDT",
            adr_share_ratio=5.0,
        ),
        fx=FxConfig(venue="bitopro", symbol="USDT/TWD"),
        sizing=SizingConfig(
            mode="notional",
            lots=1,
            leg_notional_twd=1_000_000.0,
        ),
        strategy=config.strategy,
        fees=config.fees,
        data=PairDataConfig(
            input_csv=config.input_csv,
            tw_leg_ohlcv_csv=config.tw_leg_ohlcv_csv,
            us_leg_ohlcv_csv=config.us_leg_ohlcv_csv,
            fx_ohlcv_csv=config.usdttwd_ohlcv_csv,
        ),
    )
    return replace(config, pairs=(pair,), active_pair_id=pair.id)
