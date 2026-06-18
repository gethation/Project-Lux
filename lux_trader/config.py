from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StrategyConfig:
    entry_z: float
    exit_z: float
    leg_notional_twd: float
    initial_capital_twd: float
    max_entry_delay_minutes: int
    zscore_window: int


@dataclass(frozen=True)
class FeeConfig:
    tsm_fee_bps: float
    qff_fee_per_contract_twd: float
    qff_tax_rate: float
    qff_contract_multiplier: float


@dataclass(frozen=True)
class SafetyConfig:
    allow_live_order: bool
    validate_expected_zscore: bool
    expected_zscore_tolerance: float


@dataclass(frozen=True)
class AppConfig:
    input_csv: Path
    store_path: Path
    strategy: StrategyConfig
    fees: FeeConfig
    safety: SafetyConfig


def load_config(path: Path) -> AppConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.parent

    paths = raw.get("paths", {})
    strategy = raw.get("strategy", {})
    fees = raw.get("fees", {})
    safety = raw.get("safety", {})

    input_csv = Path(paths["input_csv"]).expanduser()
    store_path = Path(paths["store_path"]).expanduser()
    if not store_path.is_absolute():
        store_path = root / store_path

    return AppConfig(
        input_csv=input_csv,
        store_path=store_path,
        strategy=StrategyConfig(
            entry_z=float(strategy.get("entry_z", 2.0)),
            exit_z=float(strategy.get("exit_z", 0.0)),
            leg_notional_twd=float(strategy.get("leg_notional_twd", 1_000_000.0)),
            initial_capital_twd=float(strategy.get("initial_capital_twd", 2_000_000.0)),
            max_entry_delay_minutes=int(strategy.get("max_entry_delay_minutes", 15)),
            zscore_window=int(strategy.get("zscore_window", 1440)),
        ),
        fees=FeeConfig(
            tsm_fee_bps=float(fees.get("tsm_fee_bps", 5.0)),
            qff_fee_per_contract_twd=float(fees.get("qff_fee_per_contract_twd", 5.0)),
            qff_tax_rate=float(fees.get("qff_tax_rate", 0.00002)),
            qff_contract_multiplier=float(fees.get("qff_contract_multiplier", 100.0)),
        ),
        safety=SafetyConfig(
            allow_live_order=bool(safety.get("allow_live_order", False)),
            validate_expected_zscore=bool(safety.get("validate_expected_zscore", True)),
            expected_zscore_tolerance=float(
                safety.get("expected_zscore_tolerance", 1e-7)
            ),
        ),
    )
