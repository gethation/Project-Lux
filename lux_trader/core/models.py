from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    SHORT_TSM_LONG_QFF = "short_tsm_long_qff"
    LONG_TSM_SHORT_QFF = "long_tsm_short_qff"


class StrategyState(StrEnum):
    FLAT = "flat"
    ENTRY_PENDING = "entry_pending"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    PAUSED = "paused"
    ERROR = "error"
    FORCED_CLOSED = "forced_closed_end_of_data"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    FILLED = "filled"
    CANCELED = "canceled"
    OPEN = "open"


class BrokerName(StrEnum):
    FUBON_QFF = "FUBON_QFF"
    BINANCE_TSM = "BINANCE_TSM"


class StrategyAction(StrEnum):
    NONE = "none"
    ENTRY_SIGNAL = "entry_signal"
    ENTRY_FILL = "entry_fill"
    ENTRY_CANCEL = "entry_cancel"
    EXIT_SIGNAL = "exit_signal"
    EXIT_FILL = "exit_fill"
    DRY_RUN_INTENT = "dry_run_intent"
    LIVE_EXECUTION = "live_execution"
    FORCE_CLOSE = "force_close"
    ERROR = "error"


@dataclass(frozen=True)
class MarketBar:
    row_index: int
    timestamp: datetime
    qff_close: float | None
    qff_close_filled: float
    tsm_twd_fair: float
    spread: float
    qff_entry_price: float | None = None
    tsm_entry_twd_fair: float | None = None
    qff_was_filled: bool = False
    qff_entry_open_was_filled: bool = False
    expected_zscore: float | None = None
    expected_zscore_valid: bool | None = None
    entry_allowed: bool = False
    close_allowed: bool = False
    friday_night_close_only: bool = False
    weekend_session_close_only: bool = False
    friday_session_end_force_close: bool = False
    qff_symbol: str | None = None
    qff_expiry: str | None = None
    contract_policy_state: str | None = None


@dataclass(frozen=True)
class IndicatorSnapshot:
    timestamp: datetime
    spread: float
    mean: float | None
    std: float | None
    zscore: float | None
    zscore_valid: bool
    entry_allowed: bool
    close_allowed: bool
    friday_night_close_only: bool
    weekend_session_close_only: bool = False
    friday_session_end_force_close: bool = False


@dataclass(frozen=True)
class PositionSizing:
    tsm_units: float
    qff_units: float
    qff_contracts: int
    raw_qff_contracts: float
    actual_leg_notional_twd: float


@dataclass(frozen=True)
class Position:
    direction: Direction
    tsm_units: float
    qff_units: float
    qff_contracts: int
    entry_tsm_twd_fair: float
    entry_qff_close: float
    entry_time: datetime
    entry_zscore: float | None


@dataclass(frozen=True)
class StrategyDecision:
    action: StrategyAction
    reason: str
    direction: Direction | None = None
    sizing: PositionSizing | None = None


@dataclass(frozen=True)
class OrderRequest:
    broker: BrokerName
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    timestamp: datetime
    row_index: int
    fee_twd: float = 0.0
    qff_symbol: str | None = None
    qff_expiry: str | None = None
    contract_policy_state: str | None = None
    order_type: str = "market"
    expected_price: float | None = None
    trigger_bid: float | None = None
    trigger_ask: float | None = None
    trigger_mid: float | None = None
    price_source: str | None = None


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    request: OrderRequest
    status: OrderStatus


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    broker: BrokerName
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    fee_twd: float
    timestamp: datetime
    row_index: int
    qff_symbol: str | None = None
    qff_expiry: str | None = None
    contract_policy_state: str | None = None


def dataclass_to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return dataclass_to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: dataclass_to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [dataclass_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [dataclass_to_jsonable(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value
