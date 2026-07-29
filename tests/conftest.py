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


# Frozen snapshot of the PoC's CCF/UMC run, committed under
# tests/fixtures/replay/. Copied rather than referenced: the PoC pipeline
# overwrites its own outputs, and a golden that moves when an upstream script is
# re-run is not a golden. The OHLCV files are trimmed to the [timestamp, open]
# columns replay actually reads.
#
# The golden assertions live in tests/integration/test_replay_golden.py, which
# loads configs/replay.fixture.ccf_umc.toml rather than rebuilding the
# parameters here -- so the committed config is itself under test.
#
# The strategy parameters in make_app_config below are NOT the golden's. They
# are arbitrary values that predate this pair, kept because a long tail of unit
# tests asserts arithmetic against them.
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "replay"
POC_CSV = _FIXTURE_DIR / "ccf_umc_spread_zscore_w2500.csv"
CCF_OHLCV = _FIXTURE_DIR / "ccf1_1m_open.csv"
UMC_OHLCV = _FIXTURE_DIR / "umc_1m_open.csv"
USD_TWD_OHLCV = _FIXTURE_DIR / "usd_twd_1m_open.csv"


@pytest.fixture
def strategy_config() -> StrategyConfig:
    # Notional sizing: these are the legacy QFF/TSM parameters that the golden
    # and the sizing-arithmetic tests are written against.
    return StrategyConfig(
        entry_z=2.0,
        exit_z=1.0,
        leg_notional_twd=1_000_000.0,
        initial_capital_twd=2_000_000.0,
        max_entry_delay_minutes=15,
        zscore_window=500,
        sizing_mode="notional",
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
        ccf_ohlcv_csv=CCF_OHLCV,
        umc_ohlcv_csv=UMC_OHLCV,
        usd_twd_ohlcv_csv=USD_TWD_OHLCV,
        strategy=StrategyConfig(
            entry_z=2.0,
            exit_z=1.0,
            leg_notional_twd=1_000_000.0,
            initial_capital_twd=2_000_000.0,
            max_entry_delay_minutes=15,
            zscore_window=500,
            # The golden was produced under notional sizing; the project default
            # is one fixed lot.
            sizing_mode="notional",
            # Explicit, not inherited: the QFF/TSM golden was produced under the
            # weekend rules, so it only reproduces under 'flat'. The project
            # default is 'none'.
            weekend_policy="flat",
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
            force_exit_grace_minutes=5,
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
