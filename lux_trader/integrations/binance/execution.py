from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Callable

from ...core.models import (
    BrokerName,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from ...execution import (
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    order_request_from_execution_leg,
)
from ...execution.intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
)
from ...market_data.parsing import parse_optional_float
from ..env import load_dotenv, require_env
from ..serialization import safe_jsonable


BINANCE_EXECUTION_SMOKE_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "BINANCE_ALLOW_LIVE_ORDER",
    "LUX_BINANCE_EXECUTION_SMOKE",
)


@dataclass(frozen=True)
class BinanceExecutionPreflight:
    open_orders: tuple[dict[str, Any], ...]
    position_quantity: float


class BinanceTsmExecutionAdapter:
    broker = BrokerName.BINANCE_TSM

    def __init__(
        self,
        symbol: str,
        env_path: Path | None = None,
        *,
        leverage: int = 1,
        margin_mode: str = "cross",
        enforce_leverage: bool = False,
        exchange: Any | None = None,
        exchange_factory: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.symbol = symbol
        self.env_path = env_path
        self.leverage = int(leverage)
        self.margin_mode = str(margin_mode).strip().lower()
        self.enforce_leverage = bool(enforce_leverage)
        self.exchange = exchange
        self.exchange_factory = exchange_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.symbol_configured = False

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        leg, reject_reason = self._select_leg(plan)
        if reject_reason is not None or leg is None:
            return self._rejected(plan, reject_reason or "invalid_binance_leg")

        exchange = self._ensure_exchange()
        original_requested_quantity = float(leg.quantity)
        try:
            normalized_quantity = normalize_binance_order_quantity(
                exchange,
                self.symbol,
                original_requested_quantity,
            )
        except ValueError as exc:
            return self._rejected(plan, str(exc))
        normalized_leg = replace(leg, quantity=normalized_quantity)
        try:
            self._configure_symbol(exchange)
        except Exception as exc:
            return self._failed_from_exception(
                plan,
                normalized_leg,
                exc,
                stage="configure_symbol",
            )
        params: dict[str, Any] = {}
        if plan.plan_type == ExecutionPlanType.EXIT:
            params["reduceOnly"] = True

        try:
            created_order = exchange.create_order(
                self.symbol,
                ExecutionOrderType.MARKET.value,
                normalized_leg.side.value,
                normalized_quantity,
                None,
                params,
            )
        except Exception as exc:
            return self._failed_from_exception(
                plan,
                normalized_leg,
                exc,
                stage="create_order",
            )

        order_id = order_id_from_order(created_order)
        if not order_id:
            return self._unknown(
                plan,
                normalized_leg,
                "Binance create_order returned no order id",
                created_order=created_order,
                fetched_order=None,
                params=params,
            )

        try:
            fetched_order = exchange.fetch_order(order_id, self.symbol)
        except Exception as exc:
            return self._unknown(
                plan,
                normalized_leg,
                f"Binance fetch_order failed: {type(exc).__name__}: {exc}",
                created_order=created_order,
                fetched_order=None,
                params=params,
                extra_payload={"fetch_error": type(exc).__name__},
            )

        return self._outcome_from_order(
            plan,
            normalized_leg,
            created_order=created_order,
            fetched_order=fetched_order,
            params=params,
            original_requested_quantity=original_requested_quantity,
        )

    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]:
        rows = self._ensure_exchange().fetch_open_orders(self.symbol) or []
        return tuple(safe_jsonable(row) for row in rows)

    def fetch_position_quantity(self) -> float:
        rows = self._ensure_exchange().fetch_positions([self.symbol]) or []
        total = 0.0
        for row in rows:
            quantity = binance_position_quantity(row, self.symbol)
            if quantity is not None:
                total += quantity
        return total

    def preflight(self) -> BinanceExecutionPreflight:
        return BinanceExecutionPreflight(
            open_orders=self.fetch_open_orders(),
            position_quantity=self.fetch_position_quantity(),
        )

    def close(self) -> None:
        close = getattr(self.exchange, "close", None)
        if callable(close):
            close()

    def _ensure_exchange(self) -> Any:
        if self.exchange is not None:
            return self.exchange
        load_dotenv(self.env_path)
        api_key = require_env("BINANCE_API_KEY")
        secret = require_env("BINANCE_SECRET")
        options = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "timeout": 30_000,
        }
        if self.exchange_factory is not None:
            self.exchange = self.exchange_factory(options)
        else:
            import ccxt

            self.exchange = ccxt.binanceusdm(options)
        load_markets = getattr(self.exchange, "load_markets", None)
        if callable(load_markets):
            load_markets()
        return self.exchange

    def _configure_symbol(self, exchange: Any) -> None:
        if not self.enforce_leverage or self.symbol_configured:
            return
        if self.margin_mode:
            current_margin_mode = fetch_current_margin_mode(exchange, self.symbol)
            if not margin_mode_matches(current_margin_mode, self.margin_mode):
                set_margin_mode = getattr(exchange, "set_margin_mode", None)
                if not callable(set_margin_mode):
                    raise RuntimeError("exchange does not support set_margin_mode")
                try:
                    set_margin_mode(self.margin_mode, self.symbol)
                except Exception as exc:
                    if not is_benign_margin_mode_error(exc):
                        raise
        current_leverage = fetch_current_leverage(exchange, self.symbol)
        if not leverage_matches(current_leverage, self.leverage):
            set_leverage = getattr(exchange, "set_leverage", None)
            if not callable(set_leverage):
                raise RuntimeError("exchange does not support set_leverage")
            set_leverage(self.leverage, self.symbol)
        self.symbol_configured = True

    def _select_leg(
        self,
        plan: PairExecutionPlan,
    ) -> tuple[ExecutionLeg | None, str | None]:
        matches = [leg for leg in plan.legs if leg.broker == BrokerName.BINANCE_TSM]
        if len(matches) != 1:
            return None, "plan must contain exactly one Binance TSM leg"
        leg = matches[0]
        if plan.order_type != ExecutionOrderType.MARKET.value:
            return leg, "plan order_type must be market"
        if leg.order_type != ExecutionOrderType.MARKET.value:
            return leg, "Binance leg order_type must be market"
        if leg.symbol != self.symbol:
            return leg, f"Binance leg symbol {leg.symbol} does not match {self.symbol}"
        if not is_positive_number(leg.quantity):
            return leg, "Binance leg quantity must be positive"
        if leg.side not in {OrderSide.BUY, OrderSide.SELL}:
            return leg, "Binance leg side must be buy or sell"
        return leg, None

    def _outcome_from_order(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        *,
        created_order: dict[str, Any],
        fetched_order: dict[str, Any],
        params: dict[str, Any],
        original_requested_quantity: float,
    ) -> ExecutionOutcome:
        order = fetched_order or created_order
        order_id = order_id_from_order(order) or order_id_from_order(created_order) or "UNKNOWN"
        status_text = str(order.get("status") or created_order.get("status") or "").lower()
        requested = float(leg.quantity)
        amount = first_number(order, "amount", "qty", "origQty") or requested
        filled = first_number(order, "filled", "executedQty", "cumQty")
        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        if filled is None and info:
            filled = first_number(info, "executedQty", "cumQty")
        if filled is None and status_text in {"closed", "filled"}:
            filled = amount
        filled = float(filled or 0.0)
        average = (
            first_number(order, "average", "avgPrice")
            or first_number(info, "avgPrice")
            or leg.expected_price
            or leg.price
        )

        outcome_status = map_binance_order_status(
            status_text=status_text,
            requested=requested,
            filled=filled,
        )
        order_status = order_status_from_outcome(outcome_status)
        order_result = OrderResult(
            order_id=str(order_id),
            request=order_request_from_execution_leg(leg),
            status=order_status,
        )

        fills: tuple[Fill, ...] = ()
        if filled > 0:
            fills = (
                Fill(
                    fill_id=f"BINANCE-FILL-{order_id}",
                    order_id=str(order_id),
                    broker=leg.broker,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=filled,
                    price=float(average),
                    fee_twd=leg.fee_twd,
                    timestamp=self.clock(),
                    row_index=leg.row_index,
                    qff_symbol=leg.qff_symbol,
                    qff_expiry=leg.qff_expiry,
                    contract_policy_state=leg.contract_policy_state,
                ),
            )

        recommended_state = None
        if outcome_status != ExecutionOutcomeStatus.FILLED:
            recommended_state = StrategyState.PAUSED

        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=outcome_status,
            message=f"Binance order {status_text or outcome_status.value}",
            orders=(order_result,),
            fills=fills,
            recommended_state=recommended_state,
            payload={
                "adapter": "binance_tsm_execution",
                "symbol": self.symbol,
                "leverage": self.leverage,
                "margin_mode": self.margin_mode,
                "enforce_leverage": self.enforce_leverage,
                "side": leg.side.value,
                "original_requested_quantity": original_requested_quantity,
                "requested_quantity": requested,
                "amount": amount,
                "filled": filled,
                "average": average,
                "exchange_status": status_text,
                "reduceOnly": bool(params.get("reduceOnly")),
                "params": safe_jsonable(params),
                "created_order": safe_jsonable(created_order),
                "fetched_order": safe_jsonable(fetched_order),
                "exchange_fee": safe_jsonable(order.get("fee")),
            },
        )

    def _rejected(self, plan: PairExecutionPlan, message: str) -> ExecutionOutcome:
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.REJECTED,
            message=message,
            recommended_state=StrategyState.PAUSED,
            payload={"adapter": "binance_tsm_execution", "reason": message},
        )

    def _failed_from_exception(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        exc: Exception,
        *,
        stage: str,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.FAILED,
            message=f"Binance {stage} failed: {type(exc).__name__}: {exc}",
            recommended_state=StrategyState.PAUSED,
            payload={
                "adapter": "binance_tsm_execution",
                "stage": stage,
                "symbol": self.symbol,
                "side": leg.side.value,
                "quantity": leg.quantity,
                "leverage": self.leverage,
                "margin_mode": self.margin_mode,
                "enforce_leverage": self.enforce_leverage,
                "error_type": type(exc).__name__,
            },
        )

    def _unknown(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        message: str,
        *,
        created_order: dict[str, Any] | None,
        fetched_order: dict[str, Any] | None,
        params: dict[str, Any],
        extra_payload: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        order_id = order_id_from_order(created_order or {}) or "UNKNOWN"
        payload = {
            "adapter": "binance_tsm_execution",
            "symbol": self.symbol,
            "side": leg.side.value,
            "quantity": leg.quantity,
            "reduceOnly": bool(params.get("reduceOnly")),
            "created_order": safe_jsonable(created_order),
            "fetched_order": safe_jsonable(fetched_order),
        }
        if extra_payload:
            payload.update(extra_payload)
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.UNKNOWN,
            message=message,
            orders=(
                OrderResult(
                    order_id=str(order_id),
                    request=order_request_from_execution_leg(leg),
                    status=OrderStatus.OPEN,
                ),
            ),
            recommended_state=StrategyState.PAUSED,
            payload=payload,
        )


def map_binance_order_status(
    *,
    status_text: str,
    requested: float,
    filled: float,
) -> ExecutionOutcomeStatus:
    full_fill = filled >= max(requested - 1e-12, 0.0)
    if status_text in {"closed", "filled"} and full_fill:
        return ExecutionOutcomeStatus.FILLED
    if filled > 0.0 and not full_fill:
        return ExecutionOutcomeStatus.PARTIAL_FILL
    if status_text in {"canceled", "cancelled", "rejected", "expired"}:
        return ExecutionOutcomeStatus.FAILED
    return ExecutionOutcomeStatus.UNKNOWN


def order_status_from_outcome(status: ExecutionOutcomeStatus) -> OrderStatus:
    if status == ExecutionOutcomeStatus.FILLED:
        return OrderStatus.FILLED
    if status == ExecutionOutcomeStatus.FAILED:
        return OrderStatus.CANCELED
    return OrderStatus.OPEN


def order_id_from_order(order: dict[str, Any] | None) -> str | None:
    if not isinstance(order, dict):
        return None
    value = (
        order.get("id")
        or order.get("orderId")
        or order.get("clientOrderId")
        or order.get("client_order_id")
    )
    if value is None and isinstance(order.get("info"), dict):
        info = order["info"]
        value = info.get("orderId") or info.get("clientOrderId")
    return str(value) if value is not None and str(value).strip() else None


def binance_position_quantity(row: dict[str, Any], expected_symbol: str) -> float | None:
    symbol = str(row.get("symbol") or "")
    if symbol != expected_symbol:
        return None
    info = row.get("info") if isinstance(row.get("info"), dict) else {}
    quantity = parse_optional_float(info.get("positionAmt")) if info else None
    if quantity is None:
        quantity = parse_optional_float(row.get("contracts"))
        side = str(row.get("side") or "").lower()
        if quantity is not None and side == "short":
            quantity = -abs(quantity)
    return quantity


def first_number(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = parse_optional_float(row.get(name))
        if value is not None:
            return value
    return None


def is_positive_number(value: float | int | None) -> bool:
    if value is None:
        return False
    numeric = float(value)
    return isfinite(numeric) and numeric > 0.0


def normalize_binance_order_quantity(
    exchange: Any,
    symbol: str,
    requested_quantity: float,
) -> float:
    if not is_positive_number(requested_quantity):
        raise ValueError("Binance order quantity must be positive")

    normalized = float(requested_quantity)
    amount_to_precision = getattr(exchange, "amount_to_precision", None)
    if callable(amount_to_precision):
        normalized_value = parse_optional_float(
            amount_to_precision(symbol, requested_quantity)
        )
        if normalized_value is None:
            raise ValueError("Binance amount precision returned an invalid quantity")
        normalized = normalized_value

    if not is_positive_number(normalized):
        raise ValueError(
            "Binance order quantity becomes zero after exchange precision"
        )

    minimum = binance_minimum_order_quantity(exchange, symbol)
    if minimum is not None and normalized + 1e-12 < minimum:
        raise ValueError(
            f"Binance order quantity {normalized} is below minimum {minimum}"
        )
    return normalized


def binance_minimum_order_quantity(exchange: Any, symbol: str) -> float | None:
    market_payload: Any = None
    market = getattr(exchange, "market", None)
    if callable(market):
        try:
            market_payload = market(symbol)
        except Exception:
            market_payload = None
    if market_payload is None:
        markets = getattr(exchange, "markets", None)
        if isinstance(markets, dict):
            market_payload = markets.get(symbol)
    if not isinstance(market_payload, dict):
        return None
    limits = market_payload.get("limits")
    if not isinstance(limits, dict):
        return None
    amount = limits.get("amount")
    if not isinstance(amount, dict):
        return None
    minimum = parse_optional_float(amount.get("min"))
    return minimum if minimum is not None and minimum > 0 else None


def fetch_current_margin_mode(exchange: Any, symbol: str) -> str | None:
    fetch_margin_mode = getattr(exchange, "fetch_margin_mode", None)
    if not callable(fetch_margin_mode):
        return None
    try:
        return parse_margin_mode(fetch_margin_mode(symbol))
    except Exception:
        return None


def fetch_current_leverage(exchange: Any, symbol: str) -> int | None:
    fetch_leverage = getattr(exchange, "fetch_leverage", None)
    if not callable(fetch_leverage):
        return None
    try:
        return parse_leverage(fetch_leverage(symbol))
    except Exception:
        return None


def parse_margin_mode(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    value = (
        payload.get("marginMode")
        or payload.get("margin_mode")
        or payload.get("mode")
        or payload.get("type")
    )
    if value is None and isinstance(payload.get("info"), dict):
        info = payload["info"]
        value = info.get("marginType") or info.get("marginMode") or info.get("mode")
    if value is None:
        return None
    return normalize_margin_mode(value)


def parse_leverage(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for name in ("leverage", "longLeverage", "shortLeverage"):
        parsed = parse_optional_float(payload.get(name))
        if parsed is not None:
            return int(parsed)
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    for name in ("leverage", "longLeverage", "shortLeverage"):
        parsed = parse_optional_float(info.get(name))
        if parsed is not None:
            return int(parsed)
    return None


def margin_mode_matches(current: str | None, desired: str) -> bool:
    if current is None:
        return False
    return normalize_margin_mode(current) == normalize_margin_mode(desired)


def leverage_matches(current: int | None, desired: int) -> bool:
    return current is not None and int(current) == int(desired)


def normalize_margin_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"crossed", "cross_margin", "cross margin"}:
        return "cross"
    return text


def is_benign_margin_mode_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "no need to change margin type" in text
        or "not need to change margin type" in text
        or "margin type cannot be changed" not in text
        and "already" in text
        and "margin" in text
    )


def binance_smoke_env_gates_open() -> dict[str, bool]:
    return {
        name: os.getenv(name, "").strip() == "1"
        for name in BINANCE_EXECUTION_SMOKE_ENV_GATES
    }


BINANCE_MANUAL_CLOSE_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "BINANCE_ALLOW_LIVE_ORDER",
    "LUX_BINANCE_MANUAL_CLOSE",
)


def binance_manual_close_env_gates_open() -> dict[str, bool]:
    return {
        name: os.getenv(name, "").strip() == "1"
        for name in BINANCE_MANUAL_CLOSE_ENV_GATES
    }
