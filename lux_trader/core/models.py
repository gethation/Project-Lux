from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    SHORT_UMC_LONG_CCF = "short_umc_long_ccf"
    LONG_UMC_SHORT_CCF = "long_umc_short_ccf"


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
    FUBON_CCF = "FUBON_CCF"
    IBKR_UMC = "IBKR_UMC"


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
    ccf_close: float | None
    ccf_close_filled: float
    umc_twd_fair: float
    spread: float
    ccf_entry_price: float | None = None
    umc_entry_twd_fair: float | None = None
    # The USD/TWD rate this bar's umc_twd_fair was converted at.
    #
    # umc_twd_fair alone cannot recover it, and the 'ibkr' fee model needs it:
    # IBKR bills in USD, so a TWD price has to be divided back out. Without it
    # on the bar, that model was unreachable from every live call site --
    # `fill_costs` raised on the first entry signal rather than charge a number
    # the configured model did not produce.
    #
    # Optional because a bar can legitimately predate the field (an older store,
    # a CSV without the column). The fee model refuses in that case rather than
    # guessing, which is the same trade this codebase makes everywhere else.
    usd_twd: float | None = None
    ccf_was_filled: bool = False
    ccf_entry_open_was_filled: bool = False
    expected_zscore: float | None = None
    expected_zscore_valid: bool | None = None
    entry_allowed: bool = False
    close_allowed: bool = False
    friday_night_close_only: bool = False
    weekend_session_close_only: bool = False
    friday_session_end_force_close: bool = False
    ccf_symbol: str | None = None
    ccf_expiry: str | None = None
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
    umc_units: float
    ccf_units: float
    ccf_contracts: int
    raw_ccf_contracts: float
    actual_leg_notional_twd: float


@dataclass(frozen=True)
class Position:
    direction: Direction
    umc_units: float
    ccf_units: float
    ccf_contracts: int
    entry_umc_twd_fair: float
    entry_ccf_close: float
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
    ccf_symbol: str | None = None
    ccf_expiry: str | None = None
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
    ccf_symbol: str | None = None
    ccf_expiry: str | None = None
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
