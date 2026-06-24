from __future__ import annotations

from pathlib import Path

import pytest

from lux_trader.config import (
    AppConfig,
    BrokerReconciliationConfig,
    ContractPolicyConfig,
    FeeConfig,
    LiveExecutionConfig,
    LiveMarketDataConfig,
    SafetyConfig,
    StrategyConfig,
    TradingCalendarConfig,
)


POC_CSV = Path(
    r"D:\Users\Documents\Proof of Concept\data\processed\qff_tsm_spread_zscore_1m_taipei_qff_session_w500.csv"
)
POC_QFF_OHLCV = Path(r"D:\Users\Documents\Proof of Concept\data\processed\qff1_1m.csv")
POC_TSM_OHLCV = Path(
    r"D:\Users\Documents\Proof of Concept\data\processed\binance_tsmusdtp_1m_taipei.csv"
)
POC_USDTTWD_OHLCV = Path(
    r"D:\Users\Documents\Proof of Concept\data\processed\bitopro_usdttwd_1m_taipei.csv"
)


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
        tsm_fee_bps=5.0,
        qff_fee_per_contract_twd=5.0,
        qff_tax_rate=0.00002,
        qff_contract_multiplier=100.0,
    )


def make_app_config(tmp_path: Path, validate_expected_zscore: bool = True) -> AppConfig:
    return AppConfig(
        input_csv=POC_CSV,
        store_path=tmp_path / "project_lux.sqlite3",
        qff_ohlcv_csv=POC_QFF_OHLCV,
        tsm_ohlcv_csv=POC_TSM_OHLCV,
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
            tsm_fee_bps=5.0,
            qff_fee_per_contract_twd=5.0,
            qff_tax_rate=0.00002,
            qff_contract_multiplier=100.0,
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
            qff_book_stale_seconds=55.0,
            sync_windows_time_on_startup=True,
            clock_skew_fail_seconds=60.0,
            windows_time_sync_timeout_seconds=15.0,
            max_leg_timestamp_skew_seconds=10.0,
            warmup_minutes=500,
            qff_product="QFF",
            qff_symbol="auto",
            binance_symbol="TSM/USDT:USDT",
            bitopro_symbol="USDT/TWD",
            fubon_env_path=None,
            taifex_qff_1m_csv=None,
            taifex_use_network=False,
            taifex_cache_dir=tmp_path / "taifex_cache",
        ),
        broker_reconciliation=BrokerReconciliationConfig(
            enabled=False,
            fail_on_mismatch=False,
            tsm_units_tolerance=1e-6,
            qff_contract_tolerance=0,
        ),
        live_execution=LiveExecutionConfig(
            enabled=False,
            require_readonly_reconciliation=True,
            max_plan_age_seconds=120,
            qff_first=True,
        ),
    )
