from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class StrategyConfig:
    entry_z: float
    exit_z: float
    leg_notional_twd: float
    initial_capital_twd: float
    max_entry_delay_minutes: int
    zscore_window: int
    tw_leg_lots: int | None = None


@dataclass(frozen=True)
class FeeConfig:
    us_leg_fee_bps: float
    tw_leg_fee_per_contract_twd: float
    tw_leg_tax_rate: float
    tw_leg_contract_multiplier: float
    us_leg_contract_multiplier: float = 5.0


@dataclass(frozen=True)
class SizingConfig:
    mode: str
    lots: int
    leg_notional_twd: float | None = None


@dataclass(frozen=True)
class TwLegConfig:
    display: str
    venue: str
    product: str
    symbol: str
    contract_multiplier: float
    taifex_1m_csv: Path | None = None


@dataclass(frozen=True)
class UsLegConfig:
    display: str
    venue: str
    symbol: str
    adr_share_ratio: float


@dataclass(frozen=True)
class FxConfig:
    venue: str
    symbol: str


@dataclass(frozen=True)
class PairDataConfig:
    input_csv: Path
    tw_leg_ohlcv_csv: Path | None
    us_leg_ohlcv_csv: Path | None
    fx_ohlcv_csv: Path | None


@dataclass(frozen=True)
class PairConfig:
    id: str
    label: str
    tw_leg: TwLegConfig
    us_leg: UsLegConfig
    fx: FxConfig
    sizing: SizingConfig
    strategy: StrategyConfig
    fees: FeeConfig
    data: PairDataConfig


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
    tw_leg_book_stale_seconds: float
    sync_windows_time_on_startup: bool
    clock_skew_fail_seconds: float
    windows_time_sync_timeout_seconds: float
    max_leg_timestamp_skew_seconds: float
    warmup_minutes: int
    tw_leg_product: str
    tw_leg_symbol: str
    binance_symbol: str
    bitopro_symbol: str
    fubon_env_path: Path | None
    taifex_tw_leg_1m_csv: Path | None
    taifex_use_network: bool
    taifex_cache_dir: Path
    # Max share of warmup minutes allowed to be futures-leg forward-filled before the
    # warmup is treated as too degraded to trade on. 1.0 disables the gate.
    warmup_forward_fill_max_ratio: float = 0.9
    # Max consecutive futures-leg trading minutes that may be forward-filled at the
    # end of the warmup window.  This independently prevents a long-stale feed
    # from passing merely because the other historical minutes are complete.
    warmup_tw_leg_max_trailing_fill_minutes: int = 5


@dataclass(frozen=True)
class BrokerReconciliationConfig:
    enabled: bool
    fail_on_mismatch: bool
    us_leg_units_tolerance: float
    tw_leg_contract_tolerance: int


@dataclass(frozen=True)
class LiveExecutionConfig:
    enabled: bool
    require_readonly_reconciliation: bool
    max_plan_age_seconds: int
    tw_leg_first: bool


@dataclass(frozen=True)
class LiveExecutionSmokeConfig:
    enabled: bool
    fubon_symbol: str
    fubon_lots: int
    binance_symbol: str
    us_leg_units: float
    tw_leg_expiry: str | None = None


@dataclass(frozen=True)
class BinanceExecutionConfig:
    leverage: int
    margin_mode: str
    enforce_leverage: bool


@dataclass(frozen=True)
class MarginManagementConfig:
    # Semi-automatic dual-account margin policy: the system only computes and
    # reports transfer guidance; transfers/closes stay manual. Thresholds are
    # ratios of account equity over per-leg notional (leg_notional_twd);
    # defaults come from the PoC margin_management_analysis simulation.
    enabled: bool = False
    check_time: str = "10:00"
    red_line_interval_minutes: int = 15
    binance_transfer_ratio: float = 0.11
    binance_red_line_ratio: float = 0.05
    fubon_transfer_ratio: float = 0.20
    fubon_red_line_ratio: float = 0.135
    target_ratio: float = 0.30
    red_line_maint_multiplier: float = 1.3
    # 0 falls back to strategy.leg_notional_twd.
    leg_notional_twd: float = 0.0


@dataclass(frozen=True)
class NtfyConfig:
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    status_topic: str = ""
    trades_topic: str = ""
    errors_topic: str = ""
    request_timeout_seconds: float = 3.0


@dataclass(frozen=True)
class AppConfig:
    input_csv: Path
    store_path: Path
    tw_leg_ohlcv_csv: Path | None
    us_leg_ohlcv_csv: Path | None
    usdttwd_ohlcv_csv: Path | None
    strategy: StrategyConfig
    fees: FeeConfig
    safety: SafetyConfig
    contract_policy: ContractPolicyConfig
    trading_calendar: TradingCalendarConfig
    live: LiveMarketDataConfig
    broker_reconciliation: BrokerReconciliationConfig
    live_execution: LiveExecutionConfig
    live_execution_smoke: LiveExecutionSmokeConfig
    binance_execution: BinanceExecutionConfig = field(
        default_factory=lambda: BinanceExecutionConfig(
            leverage=1,
            margin_mode="cross",
            enforce_leverage=True,
        )
    )
    margin_management: MarginManagementConfig = field(
        default_factory=MarginManagementConfig
    )
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    pairs: tuple[PairConfig, ...] = ()
    active_pair_id: str | None = None

    @property
    def active_pair(self) -> PairConfig:
        for pair in self.pairs:
            if pair.id == self.active_pair_id:
                return pair
        raise RuntimeError("No active pair is configured")

    def store_identity(self) -> dict[str, str]:
        pair = self.active_pair
        return {
            "pair_id": pair.id,
            "pair_label": pair.label,
            "tw_leg_display": pair.tw_leg.display,
            "us_leg_display": pair.us_leg.display,
            "tw_leg_venue": pair.tw_leg.venue,
            "us_leg_venue": pair.us_leg.venue,
        }


def load_config(path: Path, *, pair_id: str | None = None) -> AppConfig:
    path = path.expanduser().resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = config_path_root(path)

    paths = raw.get("paths", {})
    pairs_raw = raw.get("pairs")
    if not isinstance(pairs_raw, list) or not pairs_raw:
        raise RuntimeError("Config must declare at least one [[pairs]] entry")
    pairs = tuple(load_pair_config(item, root, index) for index, item in enumerate(pairs_raw))
    pair_ids = [pair.id for pair in pairs]
    if len(pair_ids) != len(set(pair_ids)):
        raise RuntimeError("Config [[pairs]] ids must be unique")
    if pair_id is None:
        if len(pairs) != 1:
            raise RuntimeError("A pair_id is required when more than one pair is configured")
        active_pair = pairs[0]
    else:
        active_pair = next((pair for pair in pairs if pair.id == pair_id), None)
        if active_pair is None:
            raise RuntimeError(
                f"Pair {pair_id!r} is not configured; available pairs: {pair_ids}"
            )

    safety = raw.get("safety", {})
    contract_policy = raw.get("contract_policy", {})
    trading_calendar = raw.get("trading_calendar", {})
    live = raw.get("live_market_data", {})
    broker_reconciliation = raw.get("broker_reconciliation", {})
    live_execution = raw.get("live_execution", {})
    live_execution_smoke = raw.get("live_execution_smoke", {})
    binance_execution = raw.get("binance_execution", {})
    margin_management = raw.get("margin_management", {})
    ntfy = raw.get("ntfy", {})

    store_path = Path(paths["store_path"]).expanduser()
    if not store_path.is_absolute():
        store_path = root / store_path
    fubon_env_path = optional_path(live.get("fubon_env_path"), root)
    taifex_cache_dir = required_path(
        live.get("taifex_cache_dir", r"data\taifex_cache"), root
    )

    return AppConfig(
        input_csv=active_pair.data.input_csv,
        store_path=store_path,
        tw_leg_ohlcv_csv=active_pair.data.tw_leg_ohlcv_csv,
        us_leg_ohlcv_csv=active_pair.data.us_leg_ohlcv_csv,
        usdttwd_ohlcv_csv=active_pair.data.fx_ohlcv_csv,
        strategy=active_pair.strategy,
        fees=active_pair.fees,
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
            tw_leg_book_stale_seconds=float(live.get("tw_leg_book_stale_seconds", 55.0)),
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
            tw_leg_product=active_pair.tw_leg.product,
            tw_leg_symbol=active_pair.tw_leg.symbol,
            binance_symbol=active_pair.us_leg.symbol,
            bitopro_symbol=active_pair.fx.symbol,
            fubon_env_path=fubon_env_path,
            taifex_tw_leg_1m_csv=active_pair.tw_leg.taifex_1m_csv,
            taifex_use_network=bool(live.get("taifex_use_network", True)),
            taifex_cache_dir=taifex_cache_dir,
            warmup_forward_fill_max_ratio=float(
                live.get("warmup_forward_fill_max_ratio", 0.9)
            ),
            warmup_tw_leg_max_trailing_fill_minutes=int(
                live.get("warmup_tw_leg_max_trailing_fill_minutes", 5)
            ),
        ),
        broker_reconciliation=BrokerReconciliationConfig(
            enabled=bool(broker_reconciliation.get("enabled", False)),
            fail_on_mismatch=bool(
                broker_reconciliation.get("fail_on_mismatch", False)
            ),
            us_leg_units_tolerance=float(
                broker_reconciliation.get("us_leg_units_tolerance", 1e-6)
            ),
            tw_leg_contract_tolerance=int(
                broker_reconciliation.get("tw_leg_contract_tolerance", 0)
            ),
        ),
        live_execution=LiveExecutionConfig(
            enabled=bool(live_execution.get("enabled", False)),
            require_readonly_reconciliation=bool(
                live_execution.get("require_readonly_reconciliation", True)
            ),
            max_plan_age_seconds=int(live_execution.get("max_plan_age_seconds", 120)),
            tw_leg_first=bool(live_execution.get("tw_leg_first", True)),
        ),
        live_execution_smoke=LiveExecutionSmokeConfig(
            enabled=bool(live_execution_smoke.get("enabled", False)),
            fubon_symbol=str(
                live_execution_smoke.get(
                    "fubon_symbol",
                    active_pair.tw_leg.symbol,
                )
            ).strip(),
            fubon_lots=optional_positive_int(
                live_execution_smoke.get(
                    "fubon_lots",
                    live_execution.get("smoke_test_fubon_lots", 1),
                ),
                "live_execution_smoke.fubon_lots",
            )
            or 1,
            binance_symbol=str(
                live_execution_smoke.get(
                    "binance_symbol",
                    active_pair.us_leg.symbol,
                )
            ).strip(),
            us_leg_units=optional_positive_float(
                live_execution_smoke.get(
                    "us_leg_units",
                    live_execution.get("smoke_test_us_leg_units", 0.1),
                ),
                "live_execution_smoke.us_leg_units",
            )
            or 0.1,
            tw_leg_expiry=optional_text(live_execution_smoke.get("tw_leg_expiry")),
        ),
        binance_execution=BinanceExecutionConfig(
            leverage=int(binance_execution.get("leverage", 1)),
            margin_mode=str(binance_execution.get("margin_mode", "cross")).strip().lower(),
            enforce_leverage=bool(binance_execution.get("enforce_leverage", True)),
        ),
        margin_management=MarginManagementConfig(
            enabled=bool(margin_management.get("enabled", False)),
            check_time=str(margin_management.get("check_time", "10:00")),
            red_line_interval_minutes=int(
                margin_management.get("red_line_interval_minutes", 15)
            ),
            binance_transfer_ratio=float(
                margin_management.get("binance_transfer_ratio", 0.11)
            ),
            binance_red_line_ratio=float(
                margin_management.get("binance_red_line_ratio", 0.05)
            ),
            fubon_transfer_ratio=float(
                margin_management.get("fubon_transfer_ratio", 0.20)
            ),
            fubon_red_line_ratio=float(
                margin_management.get("fubon_red_line_ratio", 0.135)
            ),
            target_ratio=float(margin_management.get("target_ratio", 0.30)),
            red_line_maint_multiplier=float(
                margin_management.get("red_line_maint_multiplier", 1.3)
            ),
            leg_notional_twd=float(margin_management.get("leg_notional_twd", 0.0)),
        ),
        ntfy=load_ntfy_config(ntfy),
        pairs=pairs,
        active_pair_id=active_pair.id,
    )


def load_pair_config(raw: object, root: Path, index: int) -> PairConfig:
    if not isinstance(raw, dict):
        raise RuntimeError(f"pairs[{index}] must be a TOML table")
    prefix = f"pairs[{index}]"
    pair_id = required_text(raw.get("id"), f"{prefix}.id")
    label = required_text(raw.get("label"), f"{prefix}.label")
    tw_raw = require_table(raw.get("tw_leg"), f"{prefix}.tw_leg")
    us_raw = require_table(raw.get("us_leg"), f"{prefix}.us_leg")
    fx_raw = require_table(raw.get("fx"), f"{prefix}.fx")
    sizing_raw = require_table(raw.get("sizing"), f"{prefix}.sizing")
    strategy_raw = require_table(raw.get("strategy"), f"{prefix}.strategy")
    fees_raw = require_table(raw.get("fees"), f"{prefix}.fees")
    data_raw = require_table(raw.get("data"), f"{prefix}.data")

    sizing_mode = str(sizing_raw.get("mode", "fixed_lots")).strip().lower()
    if sizing_mode not in {"fixed_lots", "notional"}:
        raise RuntimeError(
            f"{prefix}.sizing.mode must be 'fixed_lots' or 'notional'"
        )
    lots = optional_positive_int(sizing_raw.get("lots", 1), f"{prefix}.sizing.lots")
    assert lots is not None
    leg_notional = optional_positive_float(
        sizing_raw.get("leg_notional_twd"),
        f"{prefix}.sizing.leg_notional_twd",
    )
    if sizing_mode == "notional" and leg_notional is None:
        raise RuntimeError(
            f"{prefix}.sizing.leg_notional_twd is required when mode = 'notional'"
        )
    sizing = SizingConfig(
        mode=sizing_mode,
        lots=lots,
        leg_notional_twd=leg_notional,
    )

    tw_multiplier = required_positive_float(
        tw_raw.get("contract_multiplier"),
        f"{prefix}.tw_leg.contract_multiplier",
    )
    adr_share_ratio = required_positive_float(
        us_raw.get("adr_share_ratio"),
        f"{prefix}.us_leg.adr_share_ratio",
    )
    tw_leg = TwLegConfig(
        display=required_text(tw_raw.get("display"), f"{prefix}.tw_leg.display"),
        venue=required_text(tw_raw.get("venue"), f"{prefix}.tw_leg.venue").lower(),
        product=required_text(tw_raw.get("product"), f"{prefix}.tw_leg.product").upper(),
        symbol=str(tw_raw.get("symbol", "auto")).strip(),
        contract_multiplier=tw_multiplier,
        taifex_1m_csv=optional_path(tw_raw.get("taifex_1m_csv"), root),
    )
    us_leg = UsLegConfig(
        display=required_text(us_raw.get("display"), f"{prefix}.us_leg.display"),
        venue=required_text(us_raw.get("venue"), f"{prefix}.us_leg.venue").lower(),
        symbol=required_text(us_raw.get("symbol"), f"{prefix}.us_leg.symbol"),
        adr_share_ratio=adr_share_ratio,
    )
    strategy = StrategyConfig(
        entry_z=float(strategy_raw.get("entry_z", 2.0)),
        exit_z=float(strategy_raw.get("exit_z", 1.0)),
        leg_notional_twd=float(leg_notional or 0.0),
        initial_capital_twd=float(
            strategy_raw.get("initial_capital_twd", 2_000_000.0)
        ),
        max_entry_delay_minutes=int(
            strategy_raw.get("max_entry_delay_minutes", 15)
        ),
        zscore_window=int(strategy_raw.get("zscore_window", 500)),
        tw_leg_lots=lots if sizing_mode == "fixed_lots" else None,
    )
    fees = FeeConfig(
        us_leg_fee_bps=float(fees_raw.get("us_leg_fee_bps", 5.0)),
        tw_leg_fee_per_contract_twd=float(
            fees_raw.get("tw_leg_fee_per_contract_twd", 5.0)
        ),
        tw_leg_tax_rate=float(fees_raw.get("tw_leg_tax_rate", 0.00002)),
        tw_leg_contract_multiplier=tw_multiplier,
        us_leg_contract_multiplier=adr_share_ratio,
    )
    return PairConfig(
        id=pair_id,
        label=label,
        tw_leg=tw_leg,
        us_leg=us_leg,
        fx=FxConfig(
            venue=required_text(fx_raw.get("venue"), f"{prefix}.fx.venue").lower(),
            symbol=required_text(fx_raw.get("symbol"), f"{prefix}.fx.symbol"),
        ),
        sizing=sizing,
        strategy=strategy,
        fees=fees,
        data=PairDataConfig(
            input_csv=required_path(data_raw.get("input_csv", ""), root),
            tw_leg_ohlcv_csv=optional_path(data_raw.get("tw_leg_ohlcv_csv"), root),
            us_leg_ohlcv_csv=optional_path(data_raw.get("us_leg_ohlcv_csv"), root),
            fx_ohlcv_csv=optional_path(data_raw.get("fx_ohlcv_csv"), root),
        ),
    )


def load_ntfy_config(raw: dict[str, object]) -> NtfyConfig:
    enabled = bool(raw.get("enabled", False))
    server_url = str(raw.get("server_url", "https://ntfy.sh")).strip().rstrip("/")
    status_topic = str(raw.get("status_topic", "")).strip().strip("/")
    trades_topic = str(raw.get("trades_topic", "")).strip().strip("/")
    errors_topic = str(raw.get("errors_topic", "")).strip().strip("/")
    timeout = float(raw.get("request_timeout_seconds", 3.0))
    if enabled:
        if not server_url.startswith(("https://", "http://")):
            raise ValueError("ntfy.server_url must start with http:// or https://")
        missing = [
            name
            for name, value in (
                ("status_topic", status_topic),
                ("trades_topic", trades_topic),
                ("errors_topic", errors_topic),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "ntfy is enabled but topic names are missing: " + ", ".join(missing)
            )
        if timeout <= 0:
            raise ValueError("ntfy.request_timeout_seconds must be positive")
    return NtfyConfig(
        enabled=enabled,
        server_url=server_url,
        status_topic=status_topic,
        trades_topic=trades_topic,
        errors_topic=errors_topic,
        request_timeout_seconds=timeout,
    )


def parse_holidays(values: object) -> tuple[date, ...]:
    return parse_date_list(values, label="contract_policy.holidays")


def config_path_root(path: Path) -> Path:
    try:
        path.relative_to(PROJECT_ROOT)
    except ValueError:
        return path.parent
    return PROJECT_ROOT


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


def optional_positive_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    if parsed <= 0:
        raise RuntimeError(f"{label} must be positive when set")
    return parsed


def optional_positive_float(value: object, label: str) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    if parsed <= 0:
        raise RuntimeError(f"{label} must be positive when set")
    return parsed


def optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def require_table(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a TOML table")
    return value


def required_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise RuntimeError(f"{label} is required")
    return text


def required_positive_float(value: object, label: str) -> float:
    parsed = optional_positive_float(value, label)
    if parsed is None:
        raise RuntimeError(f"{label} is required")
    return parsed
