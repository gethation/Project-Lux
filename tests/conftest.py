from __future__ import annotations

from pathlib import Path

import pytest

from lux_trader.config import (
    AppConfig,
    BrokerReconciliationConfig,
    ContractPolicyConfig,
    FeeConfig,
    LiveExecutionConfig,
    LiveExecutionSmokeConfig,
    LiveMarketDataConfig,
    SafetyConfig,
    StrategyConfig,
    TradingCalendarConfig,
)


# LEGACY QFF/TSM fixtures -- these files really do hold QFF, Binance TSM and
# BitoPro USD/TWD data, not CCF/UMC. They stay under their true names while
# this branch rebuilds around CCF/UMC, because they are the only reference that
# can prove a refactor did not move the numbers. Phase C of
# docs/CCF_UMC_PLAN.md replaces them with a CCF/UMC golden and deletes these.
#
# Frozen snapshot of the PoC reference replay inputs, committed under
# tests/fixtures/replay/. The replay acceptance test must be deterministic and
# self-contained: the live PoC working directory rebuilds qff1_1m.csv from
# TAIFEX tick history, which only retains ~30 trading days, so the original
# reference dataset (and its 265,481 net PnL) ages out and cannot be rebuilt.
# These fixtures decouple the test from that mutable upstream. The OHLCV files
# are trimmed to the [timestamp, open] columns the replay actually reads.
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"
POC_CSV = _FIXTURE_DIR / "spread_zscore_w500.csv"
LEGACY_QFF_OHLCV = _FIXTURE_DIR / "qff1_1m_open.csv"
LEGACY_TSM_OHLCV = _FIXTURE_DIR / "binance_tsm_open.csv"
LEGACY_USDTTWD_OHLCV = _FIXTURE_DIR / "bitopro_usdttwd_open.csv"


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
        umc_fee_bps=5.0,
        ccf_fee_per_contract_twd=5.0,
        ccf_tax_rate=0.00002,
        ccf_contract_multiplier=100.0,
        umc_contract_multiplier=5.0,
    )


def make_app_config(tmp_path: Path, validate_expected_zscore: bool = True) -> AppConfig:
    return AppConfig(
        input_csv=POC_CSV,
        store_path=tmp_path / "project_lux.sqlite3",
        ccf_ohlcv_csv=LEGACY_QFF_OHLCV,
        umc_ohlcv_csv=LEGACY_TSM_OHLCV,
        usd_twd_ohlcv_csv=LEGACY_USDTTWD_OHLCV,
        strategy=StrategyConfig(
            entry_z=2.0,
            exit_z=1.0,
            leg_notional_twd=1_000_000.0,
            initial_capital_twd=2_000_000.0,
            max_entry_delay_minutes=15,
            zscore_window=500,
        ),
        fees=FeeConfig(
            umc_fee_bps=5.0,
            ccf_fee_per_contract_twd=5.0,
            ccf_tax_rate=0.00002,
            ccf_contract_multiplier=100.0,
            umc_contract_multiplier=5.0,
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
            ccf_book_stale_seconds=55.0,
            sync_windows_time_on_startup=True,
            clock_skew_fail_seconds=60.0,
            windows_time_sync_timeout_seconds=15.0,
            max_leg_timestamp_skew_seconds=10.0,
            warmup_minutes=500,
            ccf_product="CCF",
            ccf_symbol="auto",
            umc_symbol="UMC",
            fx_symbol="USD/TWD",
            fubon_env_path=None,
            taifex_ccf_1m_csv=None,
            taifex_use_network=False,
            taifex_cache_dir=tmp_path / "taifex_cache",
        ),
        broker_reconciliation=BrokerReconciliationConfig(
            enabled=False,
            fail_on_mismatch=False,
            umc_units_tolerance=1e-6,
            ccf_contract_tolerance=0,
        ),
        live_execution=LiveExecutionConfig(
            enabled=False,
            require_readonly_reconciliation=True,
            max_plan_age_seconds=120,
            ccf_first=True,
        ),
        live_execution_smoke=LiveExecutionSmokeConfig(
            enabled=False,
            fubon_symbol="TMFG6",
            fubon_lots=1,
            umc_symbol="UMC",
            umc_units=0.1,
            ccf_expiry="202607",
        ),
    )
