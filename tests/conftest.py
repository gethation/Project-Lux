from __future__ import annotations

from pathlib import Path

import pytest

from lux_trader.config import AppConfig, FeeConfig, SafetyConfig, StrategyConfig


POC_CSV = Path(
    r"D:\Users\Documents\Proof of Concept\data\processed\qff_tsm_spread_zscore_1m_taipei.csv"
)


@pytest.fixture
def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        entry_z=2.0,
        exit_z=0.0,
        leg_notional_twd=1_000_000.0,
        initial_capital_twd=2_000_000.0,
        max_entry_delay_minutes=15,
        zscore_window=1440,
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
        strategy=StrategyConfig(
            entry_z=2.0,
            exit_z=0.0,
            leg_notional_twd=1_000_000.0,
            initial_capital_twd=2_000_000.0,
            max_entry_delay_minutes=15,
            zscore_window=1440,
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
    )
