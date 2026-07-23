from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import FeeConfig, StrategyConfig
from .broker import Broker
from .fees import fill_costs
from .models import (
    BrokerName,
    Direction,
    Fill,
    IndicatorSnapshot,
    MarketBar,
    OrderRequest,
    OrderResult,
    OrderSide,
    PositionSizing,
    StrategyAction,
    StrategyState,
)
from .sizing import size_position_for_direction, us_leg_contract_twd_price


def minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def entry_direction(zscore: float, entry_z: float) -> Direction | None:
    if zscore > entry_z:
        return Direction.SHORT_US_LONG_TW
    if zscore < -entry_z:
        return Direction.LONG_US_SHORT_TW
    return None


def should_exit(zscore: float, direction: Direction, exit_z: float) -> bool:
    if direction == Direction.SHORT_US_LONG_TW:
        return zscore < -exit_z
    return zscore > exit_z


def entry_us_leg_price(bar: MarketBar) -> float:
    return bar.us_leg_entry_twd_fair if bar.us_leg_entry_twd_fair is not None else bar.us_leg_twd_fair


def entry_tw_leg_price(bar: MarketBar) -> float:
    return bar.tw_leg_entry_price if bar.tw_leg_entry_price is not None else bar.tw_leg_close_filled


@dataclass
class StrategyRuntimeState:
    state: StrategyState = StrategyState.FLAT
    position_direction: Direction | None = None
    candidate_direction: Direction | None = None
    candidate_idx: int = -1
    candidate_time: datetime | None = None
    candidate_zscore: float | None = None
    exit_signal_idx: int = -1
    exit_signal_time: datetime | None = None
    exit_signal_zscore: float | None = None
    entry_us_leg: float | None = None
    entry_tw_leg: float | None = None
    entry_zscore: float | None = None
    us_leg_units: float = 0.0
    tw_leg_units: float = 0.0
    tw_leg_contracts: int = 0
    actual_leg_notional_twd: float = 0.0
    realized_pnl: float = 0.0
    realized_fee_twd: float = 0.0
    running_max_equity: float = 0.0
    open_trade: dict[str, Any] | None = None
    trading_tw_leg_symbol: str | None = None
    trading_tw_leg_expiry: str | None = None
    eligible_active_tw_leg_symbol: str | None = None
    eligible_active_tw_leg_expiry: str | None = None
    pending_symbol_switch: bool = False
    last_warmup_symbol: str | None = None
    contract_policy_state: str | None = None
    pnl_status: str = "complete"

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "position_direction": (
                self.position_direction.value if self.position_direction else None
            ),
            "candidate_direction": (
                self.candidate_direction.value if self.candidate_direction else None
            ),
            "candidate_idx": self.candidate_idx,
            "candidate_time": self.candidate_time.isoformat()
            if self.candidate_time
            else None,
            "candidate_zscore": self.candidate_zscore,
            "exit_signal_idx": self.exit_signal_idx,
            "exit_signal_time": self.exit_signal_time.isoformat()
            if self.exit_signal_time
            else None,
            "exit_signal_zscore": self.exit_signal_zscore,
            "entry_us_leg": self.entry_us_leg,
            "entry_tw_leg": self.entry_tw_leg,
            "entry_zscore": self.entry_zscore,
            "us_leg_units": self.us_leg_units,
            "tw_leg_units": self.tw_leg_units,
            "tw_leg_contracts": self.tw_leg_contracts,
            "actual_leg_notional_twd": self.actual_leg_notional_twd,
            "realized_pnl": self.realized_pnl,
            "realized_fee_twd": self.realized_fee_twd,
            "running_max_equity": self.running_max_equity,
            "open_trade": self._serialize_trade(self.open_trade),
            "trading_tw_leg_symbol": self.trading_tw_leg_symbol,
            "trading_tw_leg_expiry": self.trading_tw_leg_expiry,
            "eligible_active_tw_leg_symbol": self.eligible_active_tw_leg_symbol,
            "eligible_active_tw_leg_expiry": self.eligible_active_tw_leg_expiry,
            "pending_symbol_switch": self.pending_symbol_switch,
            "last_warmup_symbol": self.last_warmup_symbol,
            "contract_policy_state": self.contract_policy_state,
            "pnl_status": self.pnl_status,
        }

    @staticmethod
    def _serialize_trade(trade: dict[str, Any] | None) -> dict[str, Any] | None:
        if trade is None:
            return None
        output: dict[str, Any] = {}
        for key, value in trade.items():
            output[key] = value.isoformat() if isinstance(value, datetime) else value
        return output

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any]) -> "StrategyRuntimeState":
        state = cls()
        state.state = StrategyState(payload.get("state", StrategyState.FLAT.value))
        pos_dir = payload.get("position_direction")
        cand_dir = payload.get("candidate_direction")
        state.position_direction = Direction(pos_dir) if pos_dir else None
        state.candidate_direction = Direction(cand_dir) if cand_dir else None
        state.candidate_idx = int(payload.get("candidate_idx", -1))
        state.candidate_time = parse_dt(payload.get("candidate_time"))
        state.candidate_zscore = payload.get("candidate_zscore")
        state.exit_signal_idx = int(payload.get("exit_signal_idx", -1))
        state.exit_signal_time = parse_dt(payload.get("exit_signal_time"))
        state.exit_signal_zscore = payload.get("exit_signal_zscore")
        state.entry_us_leg = payload.get("entry_us_leg")
        state.entry_tw_leg = payload.get("entry_tw_leg")
        state.entry_zscore = payload.get("entry_zscore")
        state.us_leg_units = float(payload.get("us_leg_units", 0.0))
        state.tw_leg_units = float(payload.get("tw_leg_units", 0.0))
        state.tw_leg_contracts = int(payload.get("tw_leg_contracts", 0))
        state.actual_leg_notional_twd = float(
            payload.get("actual_leg_notional_twd", 0.0)
        )
        state.realized_pnl = float(payload.get("realized_pnl", 0.0))
        state.realized_fee_twd = float(payload.get("realized_fee_twd", 0.0))
        state.running_max_equity = float(payload.get("running_max_equity", 0.0))
        state.open_trade = deserialize_trade(payload.get("open_trade"))
        state.trading_tw_leg_symbol = payload.get("trading_tw_leg_symbol")
        state.trading_tw_leg_expiry = payload.get("trading_tw_leg_expiry")
        state.eligible_active_tw_leg_symbol = payload.get("eligible_active_tw_leg_symbol")
        state.eligible_active_tw_leg_expiry = payload.get("eligible_active_tw_leg_expiry")
        state.pending_symbol_switch = bool(payload.get("pending_symbol_switch", False))
        state.last_warmup_symbol = payload.get("last_warmup_symbol")
        state.contract_policy_state = payload.get("contract_policy_state")
        state.pnl_status = str(payload.get("pnl_status", "complete"))
        return state


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def deserialize_trade(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    output = dict(payload)
    for key in (
        "entry_signal_time",
        "entry_time",
        "exit_signal_time",
        "exit_time",
    ):
        if output.get(key):
            output[key] = datetime.fromisoformat(str(output[key]))
    return output


@dataclass
class BarResult:
    action: StrategyAction
    reason: str
    orders: list[OrderResult] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trade: dict[str, Any] | None = None
    unrealized_pnl: float = 0.0
    equity: float = 0.0
    running_max_equity: float = 0.0
    drawdown_twd: float = 0.0
    drawdown_pct: float = 0.0


class PairStrategy:
    def __init__(
        self,
        strategy: StrategyConfig,
        fees: FeeConfig,
        broker: Broker,
        state: StrategyRuntimeState | None = None,
        *,
        us_leg_symbol: str,
        tw_leg_symbol: str,
        tw_leg_contract_multiplier: float,
        us_leg_contract_multiplier: float,
    ) -> None:
        self.strategy = strategy
        self.fees = fees
        self.broker = broker
        self.us_leg_symbol = us_leg_symbol
        self.tw_leg_symbol = tw_leg_symbol
        self.tw_leg_contract_multiplier = float(tw_leg_contract_multiplier)
        self.us_leg_contract_multiplier = float(us_leg_contract_multiplier)
        self.state = state or StrategyRuntimeState(
            running_max_equity=strategy.initial_capital_twd
        )

    def on_bar(self, bar: MarketBar, snapshot: IndicatorSnapshot) -> BarResult:
        action = StrategyAction.NONE
        reason = "no_action"
        orders: list[OrderResult] = []
        fills: list[Fill] = []
        trade: dict[str, Any] | None = None
        filled_this_bar = False

        if self.state.state == StrategyState.ENTRY_PENDING and bar.entry_allowed:
            if self.state.candidate_time is None:
                self.state.state = StrategyState.ERROR
                return self._bar_result(
                    action=StrategyAction.ERROR,
                    reason="entry_pending_without_candidate_time",
                    bar=bar,
                    orders=orders,
                    fills=fills,
                    trade=None,
                )
            delay = minutes_between(self.state.candidate_time, bar.timestamp)
            if delay <= self.strategy.max_entry_delay_minutes:
                action, reason, new_orders, new_fills = self._fill_entry(bar, snapshot, delay)
                orders.extend(new_orders)
                fills.extend(new_fills)
                filled_this_bar = True
            else:
                self._clear_candidate()
                self.state.state = StrategyState.FLAT
                action = StrategyAction.ENTRY_CANCEL
                reason = "entry_delay_exceeded"

        if (
            not filled_this_bar
            and self.state.state == StrategyState.EXIT_PENDING
            and bar.close_allowed
        ):
            action, reason, new_orders, new_fills, trade = self._fill_exit(
                bar=bar,
                snapshot=snapshot,
                exit_reason="zscore_exit",
            )
            orders.extend(new_orders)
            fills.extend(new_fills)
            filled_this_bar = True

        if not filled_this_bar:
            if self.state.state == StrategyState.FLAT and snapshot.entry_allowed:
                if snapshot.zscore_valid and snapshot.zscore is not None:
                    direction = entry_direction(snapshot.zscore, self.strategy.entry_z)
                    if direction is not None:
                        self.state.state = StrategyState.ENTRY_PENDING
                        self.state.candidate_direction = direction
                        self.state.candidate_idx = bar.row_index
                        self.state.candidate_time = bar.timestamp
                        self.state.candidate_zscore = snapshot.zscore
                        action = StrategyAction.ENTRY_SIGNAL
                        reason = "entry_zscore_crossed"

            elif self.state.state == StrategyState.OPEN and self.state.position_direction:
                if bar.friday_session_end_force_close and snapshot.close_allowed:
                    action, reason, new_orders, new_fills, trade = self._fill_exit(
                        bar=bar,
                        snapshot=snapshot,
                        exit_reason="friday_session_end",
                        reason="friday_session_end",
                    )
                    orders.extend(new_orders)
                    fills.extend(new_fills)
                if (
                    action == StrategyAction.NONE
                    and snapshot.close_allowed
                    and snapshot.zscore_valid
                    and snapshot.zscore is not None
                    and should_exit(
                        snapshot.zscore,
                        self.state.position_direction,
                        self.strategy.exit_z,
                    )
                ):
                    self.state.state = StrategyState.EXIT_PENDING
                    self.state.exit_signal_idx = bar.row_index
                    self.state.exit_signal_time = bar.timestamp
                    self.state.exit_signal_zscore = snapshot.zscore
                    action = StrategyAction.EXIT_SIGNAL
                    reason = "exit_zscore_crossed"

        return self._bar_result(action, reason, bar, orders, fills, trade)

    def finalize(self, bar: MarketBar, snapshot: IndicatorSnapshot) -> BarResult | None:
        if self.state.state == StrategyState.ENTRY_PENDING:
            self._clear_candidate()
            self.state.state = StrategyState.FLAT
            return self._bar_result(
                StrategyAction.ENTRY_CANCEL,
                "end_of_data_before_entry_fill",
                bar,
                [],
                [],
                None,
            )
        if self.state.position_direction is None or self.state.open_trade is None:
            return None
        action, reason, orders, fills, trade = self._fill_exit(
            bar=bar,
            snapshot=snapshot,
            exit_reason="end_of_data",
        )
        self.state.state = StrategyState.FORCED_CLOSED
        return self._bar_result(action, reason, bar, orders, fills, trade)

    def force_exit(
        self,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        *,
        exit_reason: str,
    ) -> BarResult | None:
        if self.state.position_direction is None or self.state.open_trade is None:
            return None
        action, reason, orders, fills, trade = self._fill_exit(
            bar=bar,
            snapshot=snapshot,
            exit_reason=exit_reason,
        )
        return self._bar_result(action, reason, bar, orders, fills, trade)

    def fill_pending_entry(
        self,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        *,
        reason: str = "entry_filled",
    ) -> BarResult:
        if self.state.candidate_time is None:
            self.state.state = StrategyState.ERROR
            return self._bar_result(
                StrategyAction.ERROR,
                "entry_pending_without_candidate_time",
                bar,
                [],
                [],
                None,
            )
        delay_minutes = minutes_between(self.state.candidate_time, bar.timestamp)
        action, fill_reason, orders, fills = self._fill_entry(
            bar=bar,
            snapshot=snapshot,
            delay_minutes=delay_minutes,
            reason=reason,
        )
        return self._bar_result(action, fill_reason, bar, orders, fills, None)

    def fill_pending_exit(
        self,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        *,
        exit_reason: str,
        reason: str = "exit_filled",
    ) -> BarResult:
        action, fill_reason, orders, fills, trade = self._fill_exit(
            bar=bar,
            snapshot=snapshot,
            exit_reason=exit_reason,
            reason=reason,
        )
        return self._bar_result(action, fill_reason, bar, orders, fills, trade)

    def _fill_entry(
        self,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        delay_minutes: int,
        reason: str = "entry_filled",
    ) -> tuple[StrategyAction, str, list[OrderResult], list[Fill]]:
        if self.state.candidate_direction is None:
            self.state.state = StrategyState.ERROR
            return StrategyAction.ERROR, "entry_pending_without_direction", [], []
        sizing = size_position_for_direction(
            self.state.candidate_direction,
            entry_us_leg_price(bar),
            entry_tw_leg_price(bar),
            self.strategy,
            tw_leg_contract_multiplier=self.tw_leg_contract_multiplier,
            us_leg_contract_multiplier=self.us_leg_contract_multiplier,
        )
        if sizing is None:
            self._clear_candidate()
            self.state.state = StrategyState.FLAT
            return StrategyAction.ENTRY_CANCEL, "tw_leg_contracts_rounded_to_zero", [], []

        costs = fill_costs(
            us_leg_units=sizing.us_leg_units,
            us_leg_price=entry_us_leg_price(bar),
            tw_leg_contracts=sizing.tw_leg_contracts,
            tw_leg_price=entry_tw_leg_price(bar),
            fees=self.fees,
            tw_leg_contract_multiplier=self.tw_leg_contract_multiplier,
            us_leg_contract_multiplier=self.us_leg_contract_multiplier,
        )
        orders, fills = self._place_entry_orders(
            bar,
            sizing.us_leg_units,
            sizing.tw_leg_contracts,
            costs,
        )
        result = self.apply_entry_execution(
            bar=bar,
            snapshot=snapshot,
            sizing=sizing,
            costs=costs,
            orders=orders,
            fills=fills,
            delay_minutes=delay_minutes,
            reason=reason,
        )
        return result.action, result.reason, result.orders, result.fills

    def apply_entry_execution(
        self,
        *,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        sizing: PositionSizing,
        costs: dict[str, float],
        orders: list[OrderResult],
        fills: list[Fill],
        delay_minutes: int,
        reason: str,
    ) -> BarResult:
        if self.state.candidate_direction is None:
            self.state.state = StrategyState.ERROR
            return self._bar_result(
                StrategyAction.ERROR,
                "entry_pending_without_direction",
                bar,
                [],
                [],
                None,
            )
        self.state.position_direction = self.state.candidate_direction
        self.state.entry_us_leg = entry_us_leg_price(bar)
        self.state.entry_tw_leg = entry_tw_leg_price(bar)
        self.state.entry_zscore = snapshot.zscore
        self.state.us_leg_units = sizing.us_leg_units
        self.state.tw_leg_units = sizing.tw_leg_units
        self.state.tw_leg_contracts = sizing.tw_leg_contracts
        self.state.actual_leg_notional_twd = sizing.actual_leg_notional_twd
        self.state.realized_pnl -= costs["total_fee_twd"]
        self.state.realized_fee_twd += costs["total_fee_twd"]
        self.state.open_trade = {
            "entry_signal_idx": self.state.candidate_idx,
            "entry_signal_time": self.state.candidate_time,
            "entry_signal_zscore": self.state.candidate_zscore,
            "entry_idx": bar.row_index,
            "entry_time": bar.timestamp,
            "entry_delay_minutes": delay_minutes,
            "entry_fill_zscore": snapshot.zscore,
            "direction": self.state.position_direction.value,
            "entry_us_leg_twd_fair": entry_us_leg_price(bar),
            "entry_tw_leg_close": entry_tw_leg_price(bar),
            "entry_fill_price_type": "open"
            if bar.us_leg_entry_twd_fair is not None or bar.tw_leg_entry_price is not None
            else "close",
            "entry_tw_leg_open_was_filled": bar.tw_leg_entry_open_was_filled,
            "us_leg_units": sizing.us_leg_units,
            "tw_leg_units": sizing.tw_leg_units,
            "tw_leg_contracts": sizing.tw_leg_contracts,
            "raw_tw_leg_contracts": sizing.raw_tw_leg_contracts,
            "leg_notional_twd": self.strategy.leg_notional_twd,
            "actual_leg_notional_twd": sizing.actual_leg_notional_twd,
            "tw_leg_contract_multiplier": self.tw_leg_contract_multiplier,
            "entry_us_leg_fee_twd": costs["us_leg_fee_twd"],
            "entry_tw_leg_fee_twd": costs["tw_leg_fee_twd"],
            "entry_tw_leg_tax_twd": costs["tw_leg_tax_twd"],
            "entry_fee_twd": costs["total_fee_twd"],
            "tw_leg_symbol": bar.tw_leg_symbol,
            "tw_leg_expiry": bar.tw_leg_expiry,
            "contract_policy_state": bar.contract_policy_state,
        }
        self._clear_candidate()
        self.state.state = StrategyState.OPEN
        return self._bar_result(
            StrategyAction.ENTRY_FILL,
            reason,
            bar,
            orders,
            fills,
            None,
        )

    def _fill_exit(
        self,
        *,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        exit_reason: str,
        reason: str = "exit_filled",
    ) -> tuple[StrategyAction, str, list[OrderResult], list[Fill], dict[str, Any]]:
        if self.state.open_trade is None or self.state.position_direction is None:
            self.state.state = StrategyState.ERROR
            return StrategyAction.ERROR, "exit_without_open_trade", [], [], {}

        costs = fill_costs(
            us_leg_units=self.state.us_leg_units,
            us_leg_price=bar.us_leg_twd_fair,
            tw_leg_contracts=self.state.tw_leg_contracts,
            tw_leg_price=bar.tw_leg_close_filled,
            fees=self.fees,
            tw_leg_contract_multiplier=self.tw_leg_contract_multiplier,
            us_leg_contract_multiplier=self.us_leg_contract_multiplier,
        )
        orders, fills = self._place_exit_orders(bar, costs)
        result = self.apply_exit_execution(
            bar=bar,
            snapshot=snapshot,
            costs=costs,
            orders=orders,
            fills=fills,
            exit_reason=exit_reason,
            reason=reason,
        )
        return result.action, result.reason, result.orders, result.fills, result.trade or {}

    def apply_exit_execution(
        self,
        *,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        costs: dict[str, float],
        orders: list[OrderResult],
        fills: list[Fill],
        exit_reason: str,
        reason: str,
    ) -> BarResult:
        if self.state.open_trade is None or self.state.position_direction is None:
            self.state.state = StrategyState.ERROR
            return self._bar_result(
                StrategyAction.ERROR,
                "exit_without_open_trade",
                bar,
                [],
                [],
                {},
            )
        open_trade = self.state.open_trade
        us_leg_pnl = self.state.us_leg_units * (
            us_leg_contract_twd_price(
                bar.us_leg_twd_fair, self.us_leg_contract_multiplier
            )
            - us_leg_contract_twd_price(
                float(open_trade["entry_us_leg_twd_fair"]),
                self.us_leg_contract_multiplier,
            )
        )
        tw_leg_pnl = self.state.tw_leg_units * (
            bar.tw_leg_close_filled - float(open_trade["entry_tw_leg_close"])
        )
        gross_pnl = us_leg_pnl + tw_leg_pnl
        us_leg_fee_twd = float(open_trade["entry_us_leg_fee_twd"]) + costs["us_leg_fee_twd"]
        tw_leg_fee_twd = float(open_trade["entry_tw_leg_fee_twd"]) + costs["tw_leg_fee_twd"]
        tw_leg_tax_twd = float(open_trade["entry_tw_leg_tax_twd"]) + costs["tw_leg_tax_twd"]
        total_fee_twd = float(open_trade["entry_fee_twd"]) + costs["total_fee_twd"]
        net_pnl = gross_pnl - total_fee_twd
        signal_idx = self.state.exit_signal_idx if self.state.exit_signal_idx != -1 else bar.row_index
        signal_time = self.state.exit_signal_time or bar.timestamp
        signal_zscore = (
            self.state.exit_signal_zscore
            if self.state.exit_signal_zscore is not None
            else snapshot.zscore
        )
        trade = {
            **open_trade,
            "exit_signal_idx": signal_idx,
            "exit_signal_time": signal_time,
            "exit_signal_zscore": signal_zscore,
            "exit_idx": bar.row_index,
            "exit_time": bar.timestamp,
            "exit_fill_zscore": snapshot.zscore,
            "exit_us_leg_twd_fair": bar.us_leg_twd_fair,
            "exit_tw_leg_close": bar.tw_leg_close_filled,
            "us_leg_pnl": us_leg_pnl,
            "tw_leg_pnl": tw_leg_pnl,
            "gross_pnl_twd": gross_pnl,
            "exit_us_leg_fee_twd": costs["us_leg_fee_twd"],
            "exit_tw_leg_fee_twd": costs["tw_leg_fee_twd"],
            "exit_tw_leg_tax_twd": costs["tw_leg_tax_twd"],
            "exit_fee_twd": costs["total_fee_twd"],
            "us_leg_fee_twd": us_leg_fee_twd,
            "tw_leg_fee_twd": tw_leg_fee_twd,
            "tw_leg_tax_twd": tw_leg_tax_twd,
            "total_fee_twd": total_fee_twd,
            "net_pnl_twd": net_pnl,
            "total_pnl": net_pnl,
            "exit_reason": exit_reason,
            "holding_minutes": minutes_between(open_trade["entry_time"], bar.timestamp),
        }

        self.state.realized_pnl += gross_pnl - costs["total_fee_twd"]
        self.state.realized_fee_twd += costs["total_fee_twd"]
        self.state.state = StrategyState.FLAT
        self.state.position_direction = None
        self.state.open_trade = None
        self.state.us_leg_units = 0.0
        self.state.tw_leg_units = 0.0
        self.state.tw_leg_contracts = 0
        self.state.actual_leg_notional_twd = 0.0
        self.state.entry_us_leg = None
        self.state.entry_tw_leg = None
        self.state.entry_zscore = None
        self.state.exit_signal_idx = -1
        self.state.exit_signal_time = None
        self.state.exit_signal_zscore = None
        return self._bar_result(
            StrategyAction.EXIT_FILL,
            reason,
            bar,
            orders,
            fills,
            trade,
        )

    def _place_entry_orders(
        self,
        bar: MarketBar,
        us_leg_units: float,
        tw_leg_contracts: int,
        costs: dict[str, float],
    ) -> tuple[list[OrderResult], list[Fill]]:
        requests = self.build_entry_order_requests(
            bar=bar,
            us_leg_units=us_leg_units,
            tw_leg_contracts=tw_leg_contracts,
            costs=costs,
        )
        return self._submit_order_requests(requests)

    def _place_exit_orders(
        self,
        bar: MarketBar,
        costs: dict[str, float],
    ) -> tuple[list[OrderResult], list[Fill]]:
        requests = self.build_exit_order_requests(bar=bar, costs=costs)
        return self._submit_order_requests(requests)

    def build_entry_order_requests(
        self,
        *,
        bar: MarketBar,
        us_leg_units: float,
        tw_leg_contracts: int,
        costs: dict[str, float],
    ) -> list[OrderRequest]:
        return build_pair_order_requests(
            bar=bar,
            us_leg_symbol=self.us_leg_symbol,
            tw_leg_symbol=self.tw_leg_symbol,
            us_leg_units=us_leg_units,
            tw_leg_contracts=tw_leg_contracts,
            us_leg_price=entry_us_leg_price(bar),
            tw_leg_price=entry_tw_leg_price(bar),
            us_leg_fee=costs["us_leg_fee_twd"],
            tw_leg_fee=costs["tw_leg_fee_twd"] + costs["tw_leg_tax_twd"],
        )

    def build_exit_order_requests(
        self,
        *,
        bar: MarketBar,
        costs: dict[str, float],
    ) -> list[OrderRequest]:
        return build_pair_order_requests(
            bar=bar,
            us_leg_symbol=self.us_leg_symbol,
            tw_leg_symbol=self.tw_leg_symbol,
            us_leg_units=-self.state.us_leg_units,
            tw_leg_contracts=-self.state.tw_leg_contracts,
            us_leg_price=bar.us_leg_twd_fair,
            tw_leg_price=bar.tw_leg_close_filled,
            us_leg_fee=costs["us_leg_fee_twd"],
            tw_leg_fee=costs["tw_leg_fee_twd"] + costs["tw_leg_tax_twd"],
        )

    def mark_to_market_result(
        self,
        *,
        action: StrategyAction,
        reason: str,
        bar: MarketBar,
    ) -> BarResult:
        return self._bar_result(action, reason, bar, [], [], None)

    def _submit_order_requests(
        self,
        requests: list[OrderRequest],
    ) -> tuple[list[OrderResult], list[Fill]]:
        orders: list[OrderResult] = []
        fills: list[Fill] = []
        for request in requests:
            order, fill = self.broker.place_order(request)
            orders.append(order)
            fills.append(fill)
        return orders, fills

    def _bar_result(
        self,
        action: StrategyAction,
        reason: str,
        bar: MarketBar,
        orders: list[OrderResult],
        fills: list[Fill],
        trade: dict[str, Any] | None,
    ) -> BarResult:
        unrealized = 0.0
        if self.state.position_direction is not None and self.state.entry_us_leg is not None and self.state.entry_tw_leg is not None:
            unrealized = self.state.us_leg_units * (
                us_leg_contract_twd_price(
                    bar.us_leg_twd_fair, self.us_leg_contract_multiplier
                )
                - us_leg_contract_twd_price(
                    self.state.entry_us_leg, self.us_leg_contract_multiplier
                )
            ) + self.state.tw_leg_units * (bar.tw_leg_close_filled - self.state.entry_tw_leg)
        equity = self.strategy.initial_capital_twd + self.state.realized_pnl + unrealized
        self.state.running_max_equity = max(self.state.running_max_equity, equity)
        drawdown = equity - self.state.running_max_equity
        drawdown_pct = drawdown / self.state.running_max_equity if self.state.running_max_equity else 0.0
        return BarResult(
            action=action,
            reason=reason,
            orders=orders,
            fills=fills,
            trade=trade,
            unrealized_pnl=unrealized,
            equity=equity,
            running_max_equity=self.state.running_max_equity,
            drawdown_twd=drawdown,
            drawdown_pct=drawdown_pct,
        )

    def _clear_candidate(self) -> None:
        self.state.candidate_direction = None
        self.state.candidate_idx = -1
        self.state.candidate_time = None
        self.state.candidate_zscore = None


def build_pair_order_requests(
    *,
    bar: MarketBar,
    us_leg_symbol: str,
    tw_leg_symbol: str,
    us_leg_units: float,
    tw_leg_contracts: int,
    us_leg_price: float,
    tw_leg_price: float,
    us_leg_fee: float,
    tw_leg_fee: float,
) -> list[OrderRequest]:
    return [
        OrderRequest(
            broker=BrokerName.BINANCE,
            symbol=us_leg_symbol,
            side=OrderSide.BUY if us_leg_units > 0 else OrderSide.SELL,
            quantity=abs(us_leg_units),
            price=us_leg_price,
            timestamp=bar.timestamp,
            row_index=bar.row_index,
            fee_twd=us_leg_fee,
            tw_leg_symbol=bar.tw_leg_symbol,
            tw_leg_expiry=bar.tw_leg_expiry,
            contract_policy_state=bar.contract_policy_state,
        ),
        OrderRequest(
            broker=BrokerName.FUBON,
            symbol=bar.tw_leg_symbol or tw_leg_symbol,
            side=OrderSide.BUY if tw_leg_contracts > 0 else OrderSide.SELL,
            quantity=abs(tw_leg_contracts),
            price=tw_leg_price,
            timestamp=bar.timestamp,
            row_index=bar.row_index,
            fee_twd=tw_leg_fee,
            tw_leg_symbol=bar.tw_leg_symbol,
            tw_leg_expiry=bar.tw_leg_expiry,
            contract_policy_state=bar.contract_policy_state,
        ),
    ]
