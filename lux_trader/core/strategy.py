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
from .sizing import size_position_for_direction, tsm_contract_twd_price


def minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def entry_direction(zscore: float, entry_z: float) -> Direction | None:
    if zscore > entry_z:
        return Direction.SHORT_TSM_LONG_QFF
    if zscore < -entry_z:
        return Direction.LONG_TSM_SHORT_QFF
    return None


def should_exit(zscore: float, direction: Direction, exit_z: float) -> bool:
    if direction == Direction.SHORT_TSM_LONG_QFF:
        return zscore < -exit_z
    return zscore > exit_z


def entry_tsm_price(bar: MarketBar) -> float:
    return bar.tsm_entry_twd_fair if bar.tsm_entry_twd_fair is not None else bar.tsm_twd_fair


def entry_qff_price(bar: MarketBar) -> float:
    return bar.qff_entry_price if bar.qff_entry_price is not None else bar.qff_close_filled


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
    entry_tsm: float | None = None
    entry_qff: float | None = None
    entry_zscore: float | None = None
    tsm_units: float = 0.0
    qff_units: float = 0.0
    qff_contracts: int = 0
    actual_leg_notional_twd: float = 0.0
    realized_pnl: float = 0.0
    realized_fee_twd: float = 0.0
    running_max_equity: float = 0.0
    open_trade: dict[str, Any] | None = None
    trading_qff_symbol: str | None = None
    trading_qff_expiry: str | None = None
    eligible_active_qff_symbol: str | None = None
    eligible_active_qff_expiry: str | None = None
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
            "entry_tsm": self.entry_tsm,
            "entry_qff": self.entry_qff,
            "entry_zscore": self.entry_zscore,
            "tsm_units": self.tsm_units,
            "qff_units": self.qff_units,
            "qff_contracts": self.qff_contracts,
            "actual_leg_notional_twd": self.actual_leg_notional_twd,
            "realized_pnl": self.realized_pnl,
            "realized_fee_twd": self.realized_fee_twd,
            "running_max_equity": self.running_max_equity,
            "open_trade": self._serialize_trade(self.open_trade),
            "trading_qff_symbol": self.trading_qff_symbol,
            "trading_qff_expiry": self.trading_qff_expiry,
            "eligible_active_qff_symbol": self.eligible_active_qff_symbol,
            "eligible_active_qff_expiry": self.eligible_active_qff_expiry,
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
        state.entry_tsm = payload.get("entry_tsm")
        state.entry_qff = payload.get("entry_qff")
        state.entry_zscore = payload.get("entry_zscore")
        state.tsm_units = float(payload.get("tsm_units", 0.0))
        state.qff_units = float(payload.get("qff_units", 0.0))
        state.qff_contracts = int(payload.get("qff_contracts", 0))
        state.actual_leg_notional_twd = float(
            payload.get("actual_leg_notional_twd", 0.0)
        )
        state.realized_pnl = float(payload.get("realized_pnl", 0.0))
        state.realized_fee_twd = float(payload.get("realized_fee_twd", 0.0))
        state.running_max_equity = float(payload.get("running_max_equity", 0.0))
        state.open_trade = deserialize_trade(payload.get("open_trade"))
        state.trading_qff_symbol = payload.get("trading_qff_symbol")
        state.trading_qff_expiry = payload.get("trading_qff_expiry")
        state.eligible_active_qff_symbol = payload.get("eligible_active_qff_symbol")
        state.eligible_active_qff_expiry = payload.get("eligible_active_qff_expiry")
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
        tsm_symbol: str = "TSM/USDT:USDT",
    ) -> None:
        self.strategy = strategy
        self.fees = fees
        self.broker = broker
        self.tsm_symbol = tsm_symbol
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
            entry_tsm_price(bar),
            entry_qff_price(bar),
            self.strategy,
            self.fees,
        )
        if sizing is None:
            self._clear_candidate()
            self.state.state = StrategyState.FLAT
            return StrategyAction.ENTRY_CANCEL, "qff_contracts_rounded_to_zero", [], []

        costs = fill_costs(
            tsm_units=sizing.tsm_units,
            tsm_price=entry_tsm_price(bar),
            qff_contracts=sizing.qff_contracts,
            qff_price=entry_qff_price(bar),
            fees=self.fees,
        )
        orders, fills = self._place_entry_orders(
            bar,
            sizing.tsm_units,
            sizing.qff_contracts,
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
        self.state.entry_tsm = entry_tsm_price(bar)
        self.state.entry_qff = entry_qff_price(bar)
        self.state.entry_zscore = snapshot.zscore
        self.state.tsm_units = sizing.tsm_units
        self.state.qff_units = sizing.qff_units
        self.state.qff_contracts = sizing.qff_contracts
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
            "entry_tsm_twd_fair": entry_tsm_price(bar),
            "entry_qff_close": entry_qff_price(bar),
            "entry_fill_price_type": "open"
            if bar.tsm_entry_twd_fair is not None or bar.qff_entry_price is not None
            else "close",
            "entry_qff_open_was_filled": bar.qff_entry_open_was_filled,
            "tsm_units": sizing.tsm_units,
            "qff_units": sizing.qff_units,
            "qff_contracts": sizing.qff_contracts,
            "raw_qff_contracts": sizing.raw_qff_contracts,
            "leg_notional_twd": self.strategy.leg_notional_twd,
            "actual_leg_notional_twd": sizing.actual_leg_notional_twd,
            "qff_contract_multiplier": self.fees.qff_contract_multiplier,
            "entry_tsm_fee_twd": costs["tsm_fee_twd"],
            "entry_qff_fee_twd": costs["qff_fee_twd"],
            "entry_qff_tax_twd": costs["qff_tax_twd"],
            "entry_fee_twd": costs["total_fee_twd"],
            "qff_symbol": bar.qff_symbol,
            "qff_expiry": bar.qff_expiry,
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
            tsm_units=self.state.tsm_units,
            tsm_price=bar.tsm_twd_fair,
            qff_contracts=self.state.qff_contracts,
            qff_price=bar.qff_close_filled,
            fees=self.fees,
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
        tsm_pnl = self.state.tsm_units * (
            tsm_contract_twd_price(bar.tsm_twd_fair, self.fees)
            - tsm_contract_twd_price(float(open_trade["entry_tsm_twd_fair"]), self.fees)
        )
        qff_pnl = self.state.qff_units * (
            bar.qff_close_filled - float(open_trade["entry_qff_close"])
        )
        gross_pnl = tsm_pnl + qff_pnl
        tsm_fee_twd = float(open_trade["entry_tsm_fee_twd"]) + costs["tsm_fee_twd"]
        qff_fee_twd = float(open_trade["entry_qff_fee_twd"]) + costs["qff_fee_twd"]
        qff_tax_twd = float(open_trade["entry_qff_tax_twd"]) + costs["qff_tax_twd"]
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
            "exit_tsm_twd_fair": bar.tsm_twd_fair,
            "exit_qff_close": bar.qff_close_filled,
            "tsm_pnl": tsm_pnl,
            "qff_pnl": qff_pnl,
            "gross_pnl_twd": gross_pnl,
            "exit_tsm_fee_twd": costs["tsm_fee_twd"],
            "exit_qff_fee_twd": costs["qff_fee_twd"],
            "exit_qff_tax_twd": costs["qff_tax_twd"],
            "exit_fee_twd": costs["total_fee_twd"],
            "tsm_fee_twd": tsm_fee_twd,
            "qff_fee_twd": qff_fee_twd,
            "qff_tax_twd": qff_tax_twd,
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
        self.state.tsm_units = 0.0
        self.state.qff_units = 0.0
        self.state.qff_contracts = 0
        self.state.actual_leg_notional_twd = 0.0
        self.state.entry_tsm = None
        self.state.entry_qff = None
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
        tsm_units: float,
        qff_contracts: int,
        costs: dict[str, float],
    ) -> tuple[list[OrderResult], list[Fill]]:
        requests = self.build_entry_order_requests(
            bar=bar,
            tsm_units=tsm_units,
            qff_contracts=qff_contracts,
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
        tsm_units: float,
        qff_contracts: int,
        costs: dict[str, float],
    ) -> list[OrderRequest]:
        return build_pair_order_requests(
            bar=bar,
            tsm_symbol=self.tsm_symbol,
            tsm_units=tsm_units,
            qff_contracts=qff_contracts,
            tsm_price=entry_tsm_price(bar),
            qff_price=entry_qff_price(bar),
            tsm_fee=costs["tsm_fee_twd"],
            qff_fee=costs["qff_fee_twd"] + costs["qff_tax_twd"],
        )

    def build_exit_order_requests(
        self,
        *,
        bar: MarketBar,
        costs: dict[str, float],
    ) -> list[OrderRequest]:
        return build_pair_order_requests(
            bar=bar,
            tsm_symbol=self.tsm_symbol,
            tsm_units=-self.state.tsm_units,
            qff_contracts=-self.state.qff_contracts,
            tsm_price=bar.tsm_twd_fair,
            qff_price=bar.qff_close_filled,
            tsm_fee=costs["tsm_fee_twd"],
            qff_fee=costs["qff_fee_twd"] + costs["qff_tax_twd"],
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
        if self.state.position_direction is not None and self.state.entry_tsm is not None and self.state.entry_qff is not None:
            unrealized = self.state.tsm_units * (
                tsm_contract_twd_price(bar.tsm_twd_fair, self.fees)
                - tsm_contract_twd_price(self.state.entry_tsm, self.fees)
            ) + self.state.qff_units * (bar.qff_close_filled - self.state.entry_qff)
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
    tsm_symbol: str,
    tsm_units: float,
    qff_contracts: int,
    tsm_price: float,
    qff_price: float,
    tsm_fee: float,
    qff_fee: float,
) -> list[OrderRequest]:
    return [
        OrderRequest(
            broker=BrokerName.BINANCE_TSM,
            symbol=tsm_symbol,
            side=OrderSide.BUY if tsm_units > 0 else OrderSide.SELL,
            quantity=abs(tsm_units),
            price=tsm_price,
            timestamp=bar.timestamp,
            row_index=bar.row_index,
            fee_twd=tsm_fee,
            qff_symbol=bar.qff_symbol,
            qff_expiry=bar.qff_expiry,
            contract_policy_state=bar.contract_policy_state,
        ),
        OrderRequest(
            broker=BrokerName.FUBON_QFF,
            symbol=bar.qff_symbol or "QFF",
            side=OrderSide.BUY if qff_contracts > 0 else OrderSide.SELL,
            quantity=abs(qff_contracts),
            price=qff_price,
            timestamp=bar.timestamp,
            row_index=bar.row_index,
            fee_twd=qff_fee,
            qff_symbol=bar.qff_symbol,
            qff_expiry=bar.qff_expiry,
            contract_policy_state=bar.contract_policy_state,
        ),
    ]
