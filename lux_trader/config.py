from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date


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
class ContractPolicyConfig:
    enabled: bool
    min_business_days_to_expiry: int
    force_exit_business_days_before_expiry: int
    force_exit_time: str
    holidays: tuple[date, ...]


@dataclass(frozen=True)
class TradingCalendarConfig:
    closed_dates: tuple[date, ...]


@dataclass(frozen=True)
class LiveMarketDataConfig:
    polling_seconds: float
    minute_finalize_delay_seconds: float
    stale_seconds: float
    qff_book_stale_seconds: float
    sync_windows_time_on_startup: bool
    clock_skew_fail_seconds: float
    windows_time_sync_timeout_seconds: float
    max_leg_timestamp_skew_seconds: float
    warmup_minutes: int
    qff_product: str
    qff_symbol: str
    binance_symbol: str
    bitopro_symbol: str
    fubon_env_path: Path | None
    taifex_qff_1m_csv: Path | None
    taifex_use_network: bool
    taifex_cache_dir: Path


@dataclass(frozen=True)
class BrokerReconciliationConfig:
    enabled: bool
    fail_on_mismatch: bool
    tsm_units_tolerance: float
    qff_contract_tolerance: int


@dataclass(frozen=True)
class LiveExecutionConfig:
    enabled: bool
    require_readonly_reconciliation: bool
    max_plan_age_seconds: int
    qff_first: bool


@dataclass(frozen=True)
class BinanceExecutionConfig:
    leverage: int
    margin_mode: str
    enforce_leverage: bool


@dataclass(frozen=True)
class AppConfig:
    input_csv: Path
    store_path: Path
    qff_ohlcv_csv: Path | None
    tsm_ohlcv_csv: Path | None
    usdttwd_ohlcv_csv: Path | None
    strategy: StrategyConfig
    fees: FeeConfig
    safety: SafetyConfig
    contract_policy: ContractPolicyConfig
    trading_calendar: TradingCalendarConfig
    live: LiveMarketDataConfig
    broker_reconciliation: BrokerReconciliationConfig
    live_execution: LiveExecutionConfig
    binance_execution: BinanceExecutionConfig = field(
        default_factory=lambda: BinanceExecutionConfig(
            leverage=1,
            margin_mode="cross",
            enforce_leverage=True,
        )
    )


def load_config(path: Path) -> AppConfig:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = path.parent

    paths = raw.get("paths", {})
    strategy = raw.get("strategy", {})
    fees = raw.get("fees", {})
    safety = raw.get("safety", {})
    contract_policy = raw.get("contract_policy", {})
    trading_calendar = raw.get("trading_calendar", {})
    live = raw.get("live_market_data", {})
    broker_reconciliation = raw.get("broker_reconciliation", {})
    live_execution = raw.get("live_execution", {})
    binance_execution = raw.get("binance_execution", {})

    input_csv = Path(paths.get("input_csv", "")).expanduser()
    store_path = Path(paths["store_path"]).expanduser()
    if not store_path.is_absolute():
        store_path = root / store_path
    qff_ohlcv_csv = optional_path(paths.get("qff_ohlcv_csv"), root)
    tsm_ohlcv_csv = optional_path(paths.get("tsm_ohlcv_csv"), root)
    usdttwd_ohlcv_csv = optional_path(paths.get("usdttwd_ohlcv_csv"), root)
    fubon_env_path = optional_path(live.get("fubon_env_path"), root)
    taifex_qff_1m_csv = optional_path(live.get("taifex_qff_1m_csv"), root)
    taifex_cache_dir = required_path(
        live.get("taifex_cache_dir", r"data\taifex_cache"), root
    )

    return AppConfig(
        input_csv=input_csv,
        store_path=store_path,
        qff_ohlcv_csv=qff_ohlcv_csv,
        tsm_ohlcv_csv=tsm_ohlcv_csv,
        usdttwd_ohlcv_csv=usdttwd_ohlcv_csv,
        strategy=StrategyConfig(
            entry_z=float(strategy.get("entry_z", 2.0)),
            exit_z=float(strategy.get("exit_z", 1.0)),
            leg_notional_twd=float(strategy.get("leg_notional_twd", 1_000_000.0)),
            initial_capital_twd=float(strategy.get("initial_capital_twd", 2_000_000.0)),
            max_entry_delay_minutes=int(strategy.get("max_entry_delay_minutes", 15)),
            zscore_window=int(strategy.get("zscore_window", 500)),
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
        contract_policy=ContractPolicyConfig(
            enabled=bool(contract_policy.get("enabled", True)),
            min_business_days_to_expiry=int(
                contract_policy.get("min_business_days_to_expiry", 5)
            ),
            force_exit_business_days_before_expiry=int(
                contract_policy.get("force_exit_business_days_before_expiry", 1)
            ),
            force_exit_time=str(contract_policy.get("force_exit_time", "13:35")),
            holidays=parse_holidays(contract_policy.get("holidays", [])),
        ),
        trading_calendar=TradingCalendarConfig(
            closed_dates=parse_date_list(
                trading_calendar.get(
                    "closed_dates",
                    contract_policy.get("holidays", []),
                ),
                label="trading_calendar.closed_dates",
            ),
        ),
        live=LiveMarketDataConfig(
            polling_seconds=float(live.get("polling_seconds", 1.0)),
            minute_finalize_delay_seconds=float(
                live.get("minute_finalize_delay_seconds", 1.0)
            ),
            stale_seconds=float(live.get("stale_seconds", 10.0)),
            qff_book_stale_seconds=float(live.get("qff_book_stale_seconds", 55.0)),
            sync_windows_time_on_startup=bool(
                live.get("sync_windows_time_on_startup", True)
            ),
            clock_skew_fail_seconds=float(live.get("clock_skew_fail_seconds", 60.0)),
            windows_time_sync_timeout_seconds=float(
                live.get("windows_time_sync_timeout_seconds", 15.0)
            ),
            max_leg_timestamp_skew_seconds=float(
                live.get("max_leg_timestamp_skew_seconds", 10.0)
            ),
            warmup_minutes=int(live.get("warmup_minutes", 500)),
            qff_product=str(live.get("qff_product", "QFF")).strip().upper(),
            qff_symbol=str(live.get("qff_symbol", "auto")).strip(),
            binance_symbol=str(live.get("binance_symbol", "TSM/USDT:USDT")).strip(),
            bitopro_symbol=str(live.get("bitopro_symbol", "USDT/TWD")).strip(),
            fubon_env_path=fubon_env_path,
            taifex_qff_1m_csv=taifex_qff_1m_csv,
            taifex_use_network=bool(live.get("taifex_use_network", True)),
            taifex_cache_dir=taifex_cache_dir,
        ),
        broker_reconciliation=BrokerReconciliationConfig(
            enabled=bool(broker_reconciliation.get("enabled", False)),
            fail_on_mismatch=bool(
                broker_reconciliation.get("fail_on_mismatch", False)
            ),
            tsm_units_tolerance=float(
                broker_reconciliation.get("tsm_units_tolerance", 1e-6)
            ),
            qff_contract_tolerance=int(
                broker_reconciliation.get("qff_contract_tolerance", 0)
            ),
        ),
        live_execution=LiveExecutionConfig(
            enabled=bool(live_execution.get("enabled", False)),
            require_readonly_reconciliation=bool(
                live_execution.get("require_readonly_reconciliation", True)
            ),
            max_plan_age_seconds=int(live_execution.get("max_plan_age_seconds", 120)),
            qff_first=bool(live_execution.get("qff_first", True)),
        ),
        binance_execution=BinanceExecutionConfig(
            leverage=int(binance_execution.get("leverage", 1)),
            margin_mode=str(binance_execution.get("margin_mode", "cross")).strip().lower(),
            enforce_leverage=bool(binance_execution.get("enforce_leverage", True)),
        ),
    )


def parse_holidays(values: object) -> tuple[date, ...]:
    return parse_date_list(values, label="contract_policy.holidays")


def parse_date_list(values: object, *, label: str) -> tuple[date, ...]:
    if values is None:
        return ()
    if not isinstance(values, list):
        raise RuntimeError(f"{label} must be a list of YYYY-MM-DD strings")
    return tuple(date.fromisoformat(str(value)) for value in values)


def required_path(value: object, root: Path) -> Path:
    path = Path(str(value).strip()).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def optional_path(value: object, root: Path) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root / path
    return path
