from __future__ import annotations

import os
import time
import uuid
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
        sleep: Callable[[float], None] | None = None,
        max_poll_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
        recovery_attempts: int = 3,
    ) -> None:
        self.symbol = symbol
        self.env_path = env_path
        self.leverage = int(leverage)
        self.margin_mode = str(margin_mode).strip().lower()
        self.enforce_leverage = bool(enforce_leverage)
        self.exchange = exchange
        self.exchange_factory = exchange_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleep = sleep or time.sleep
        self.max_poll_seconds = float(max_poll_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.recovery_attempts = max(1, int(recovery_attempts))
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
        # Pre-assigned client order id: if create_order times out after the
        # request reached the matching engine, the order can still be recovered
        # by origClientOrderId instead of being misreported as FAILED.
        client_order_id = make_binance_client_order_id()
        params["newClientOrderId"] = client_order_id

        position_before, position_errors = self._safe_fetch_position_quantity(
            stage="before_order"
        )

        submit_started_at = self.clock()
        recovery: dict[str, Any] | None = None
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
            recovery_status, recovered_order, recovery_errors = (
                self._recover_order_after_create_error(exchange, client_order_id)
            )
            recovery = {
                "create_error_type": type(exc).__name__,
                "create_error": str(exc),
                "status": recovery_status,
                "errors": tuple(recovery_errors),
            }
            if recovery_status == "not_found":
                # The order never reached the engine: a genuine failure.
                outcome = self._failed_from_exception(
                    plan,
                    normalized_leg,
                    exc,
                    stage="create_order",
                )
                outcome.payload["recovery"] = safe_jsonable(recovery)
                outcome.payload["client_order_id"] = client_order_id
                return outcome
            if recovery_status != "found" or recovered_order is None:
                # The order may exist; consult the position delta before
                # settling on UNKNOWN.
                return self._unknown_after_create_error(
                    plan,
                    normalized_leg,
                    exc,
                    params=params,
                    client_order_id=client_order_id,
                    recovery=recovery,
                    position_before=position_before,
                    position_errors=position_errors,
                    submit_started_at=submit_started_at,
                    original_requested_quantity=original_requested_quantity,
                )
            created_order = recovered_order
        submit_finished_at = self.clock()

        order_id = order_id_from_order(created_order)
        if not order_id:
            return self._unknown(
                plan,
                normalized_leg,
                "Binance create_order returned no order id",
                created_order=created_order,
                fetched_order=None,
                params=params,
                submit_started_at=submit_started_at,
                submit_finished_at=submit_finished_at,
                extra_payload={"client_order_id": client_order_id},
            )

        fetched_order, poll_errors = self._poll_fetched_order(
            exchange,
            order_id,
            requested=float(normalized_leg.quantity),
        )
        if fetched_order is None and not binance_order_is_final(
            created_order, requested=float(normalized_leg.quantity)
        ):
            # Every fetch attempt failed and the create response alone cannot
            # confirm the fill; fall back to the position delta as evidence.
            position_delta = self._position_delta_fallback(
                normalized_leg, position_before, position_errors
            )
            return self._outcome_from_order(
                plan,
                normalized_leg,
                created_order=created_order,
                fetched_order=None,
                params=params,
                original_requested_quantity=original_requested_quantity,
                submit_started_at=submit_started_at,
                submit_finished_at=submit_finished_at,
                poll_errors=tuple(poll_errors),
                position_before=position_before,
                position_delta=position_delta,
                position_errors=tuple(position_errors),
                client_order_id=client_order_id,
                recovery=recovery,
            )

        order_for_fill_check = fetched_order or created_order
        position_delta = None
        if not binance_order_is_filled(
            order_for_fill_check, requested=float(normalized_leg.quantity)
        ):
            position_delta = self._position_delta_fallback(
                normalized_leg, position_before, position_errors
            )
        return self._outcome_from_order(
            plan,
            normalized_leg,
            created_order=created_order,
            fetched_order=fetched_order,
            params=params,
            original_requested_quantity=original_requested_quantity,
            submit_started_at=submit_started_at,
            submit_finished_at=submit_finished_at,
            poll_errors=tuple(poll_errors),
            position_before=position_before,
            position_delta=position_delta,
            position_errors=tuple(position_errors),
            client_order_id=client_order_id,
            recovery=recovery,
        )

    def _poll_fetched_order(
        self,
        exchange: Any,
        order_id: str,
        *,
        requested: float,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Bounded fetch_order loop: keep polling while the order is working
        (Binance market orders normally fill within one attempt); terminal
        statuses (closed/canceled/rejected/expired) exit immediately."""
        attempts = max(
            1,
            int(self.max_poll_seconds / max(self.poll_interval_seconds, 0.001)) + 1,
        )
        errors: list[dict[str, Any]] = []
        fetched: dict[str, Any] | None = None
        for attempt in range(attempts):
            try:
                fetched = exchange.fetch_order(order_id, self.symbol)
            except Exception as exc:
                errors.append(
                    {
                        "attempt": attempt + 1,
                        "stage": "fetch_order",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            if fetched is not None and binance_order_is_final(
                fetched, requested=requested
            ):
                break
            if attempt < attempts - 1 and self.poll_interval_seconds > 0:
                self.sleep(self.poll_interval_seconds)
        return fetched, errors

    def _recover_order_after_create_error(
        self,
        exchange: Any,
        client_order_id: str,
    ) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
        """Determine whether a create_order exception left a live order behind.

        Returns ("found", order, errors) when the order exists, ("not_found",
        None, errors) when Binance confirms it does not, and ("error", None,
        errors) when the lookup itself kept failing (order may exist)."""
        errors: list[dict[str, Any]] = []
        for attempt in range(self.recovery_attempts):
            try:
                order = exchange.fetch_order(
                    None,
                    self.symbol,
                    {"origClientOrderId": client_order_id},
                )
                if order:
                    return "found", order, errors
            except Exception as exc:
                if is_binance_order_not_found(exc):
                    return "not_found", None, errors
                errors.append(
                    {
                        "attempt": attempt + 1,
                        "stage": "recover_by_client_order_id",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            if attempt < self.recovery_attempts - 1 and self.poll_interval_seconds > 0:
                self.sleep(self.poll_interval_seconds)
        return "error", None, errors

    def _safe_fetch_position_quantity(
        self,
        *,
        stage: str,
    ) -> tuple[float | None, list[dict[str, Any]]]:
        try:
            return self.fetch_position_quantity(), []
        except Exception as exc:
            return None, [
                {
                    "stage": stage,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            ]

    def _position_delta_fallback(
        self,
        leg: ExecutionLeg,
        position_before: float | None,
        position_errors: list[dict[str, Any]],
    ) -> dict[str, float] | None:
        if position_before is None:
            return None
        position_after, after_errors = self._safe_fetch_position_quantity(
            stage="after_order"
        )
        position_errors.extend(after_errors)
        if position_after is None:
            return None
        return binance_position_delta_confirmation(
            leg=leg,
            requested=float(leg.quantity),
            before=position_before,
            after=position_after,
        )

    def _unknown_after_create_error(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        exc: Exception,
        *,
        params: dict[str, Any],
        client_order_id: str,
        recovery: dict[str, Any],
        position_before: float | None,
        position_errors: list[dict[str, Any]],
        submit_started_at: datetime,
        original_requested_quantity: float,
    ) -> ExecutionOutcome:
        position_delta = self._position_delta_fallback(
            leg, position_before, position_errors
        )
        delta_lot = float((position_delta or {}).get("confirmed_fill_lot") or 0.0)
        if delta_lot > 0:
            # The account moved in the order's direction: treat the delta as
            # fill evidence exactly like the Fubon fallback path.
            return self._outcome_from_order(
                plan,
                leg,
                created_order={},
                fetched_order=None,
                params=params,
                original_requested_quantity=original_requested_quantity,
                submit_started_at=submit_started_at,
                submit_finished_at=self.clock(),
                position_before=position_before,
                position_delta=position_delta,
                position_errors=tuple(position_errors),
                client_order_id=client_order_id,
                recovery=recovery,
            )
        return self._unknown(
            plan,
            leg,
            f"Binance create_order outcome unknown: {type(exc).__name__}: {exc}",
            created_order=None,
            fetched_order=None,
            params=params,
            submit_started_at=submit_started_at,
            submit_finished_at=self.clock(),
            extra_payload={
                "client_order_id": client_order_id,
                "recovery": safe_jsonable(recovery),
                "position_before": position_before,
                "position_delta": position_delta,
                "position_errors": tuple(position_errors),
            },
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
            # Futures-only API keys have no spot-wallet permission; stop
            # load_markets from calling sapi/v1/capital/config/getall
            # (it fails with -2015 and is not needed for USDM futures).
            "options": {"fetchCurrencies": False},
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
        fetched_order: dict[str, Any] | None,
        params: dict[str, Any],
        original_requested_quantity: float,
        submit_started_at: datetime,
        submit_finished_at: datetime,
        poll_errors: tuple[dict[str, Any], ...] = (),
        position_before: float | None = None,
        position_delta: dict[str, float] | None = None,
        position_errors: tuple[dict[str, Any], ...] = (),
        client_order_id: str | None = None,
        recovery: dict[str, Any] | None = None,
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
        fill_source = "order_result" if filled > 0 else None
        position_delta_fill = float(
            (position_delta or {}).get("confirmed_fill_lot") or 0.0
        )
        if position_delta_fill > filled + 1e-12:
            filled = position_delta_fill
            fill_source = "position_delta"
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
                "submit_started_at": submit_started_at,
                "submit_finished_at": submit_finished_at,
                "original_requested_quantity": original_requested_quantity,
                "requested_quantity": requested,
                "amount": amount,
                "filled": filled,
                "fill_source": fill_source,
                "average": average,
                "exchange_status": status_text,
                "reduceOnly": bool(params.get("reduceOnly")),
                "params": safe_jsonable(params),
                "created_order": safe_jsonable(created_order),
                "fetched_order": safe_jsonable(fetched_order),
                "exchange_fee": safe_jsonable(order.get("fee")),
                "poll_errors": tuple(poll_errors),
                "position_before": position_before,
                "position_delta": position_delta,
                "position_errors": tuple(position_errors),
                "client_order_id": client_order_id,
                "recovery": safe_jsonable(recovery),
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
        submit_started_at: datetime | None = None,
        submit_finished_at: datetime | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> ExecutionOutcome:
        order_id = order_id_from_order(created_order or {}) or "UNKNOWN"
        payload = {
            "adapter": "binance_tsm_execution",
            "symbol": self.symbol,
            "side": leg.side.value,
            "quantity": leg.quantity,
            "submit_started_at": submit_started_at,
            "submit_finished_at": submit_finished_at,
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


BINANCE_TERMINAL_NEGATIVE_STATUSES = {"canceled", "cancelled", "rejected", "expired"}


def map_binance_order_status(
    *,
    status_text: str,
    requested: float,
    filled: float,
) -> ExecutionOutcomeStatus:
    full_fill = filled >= max(requested - 1e-12, 0.0)
    if status_text in {"closed", "filled"} and full_fill:
        return ExecutionOutcomeStatus.FILLED
    if filled > 0.0 and full_fill:
        # Position-delta / recovery evidence can confirm a full fill even when
        # the last known order status is still a working one.
        return ExecutionOutcomeStatus.FILLED
    if filled > 0.0 and not full_fill:
        return ExecutionOutcomeStatus.PARTIAL_FILL
    if status_text in BINANCE_TERMINAL_NEGATIVE_STATUSES:
        return ExecutionOutcomeStatus.FAILED
    return ExecutionOutcomeStatus.UNKNOWN


def make_binance_client_order_id() -> str:
    # Binance clientOrderId: <= 36 chars of [A-Za-z0-9._:/-].
    return f"LUX-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def is_binance_order_not_found(exc: Exception) -> bool:
    if type(exc).__name__ == "OrderNotFound":
        return True
    text = str(exc).lower()
    # Binance error -2013: "Order does not exist."
    return "-2013" in text or "does not exist" in text


def binance_order_filled_quantity(order: dict[str, Any] | None) -> float:
    if not isinstance(order, dict):
        return 0.0
    filled = first_number(order, "filled", "executedQty", "cumQty")
    if filled is None:
        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        filled = first_number(info, "executedQty", "cumQty")
    status_text = str(order.get("status") or "").lower()
    if filled is None and status_text in {"closed", "filled"}:
        filled = first_number(order, "amount", "qty", "origQty")
    return float(filled or 0.0)


def binance_order_is_filled(order: dict[str, Any] | None, *, requested: float) -> bool:
    return binance_order_filled_quantity(order) >= max(requested - 1e-12, 0.0)


def binance_order_is_final(order: dict[str, Any] | None, *, requested: float) -> bool:
    if not isinstance(order, dict):
        return False
    if binance_order_is_filled(order, requested=requested):
        return True
    status_text = str(order.get("status") or "").lower()
    return status_text in BINANCE_TERMINAL_NEGATIVE_STATUSES


def signed_binance_quantity_delta(side: OrderSide, quantity: float) -> float:
    return float(quantity) if side == OrderSide.BUY else -float(quantity)


def binance_position_delta_confirmation(
    *,
    leg: ExecutionLeg,
    requested: float,
    before: float,
    after: float,
) -> dict[str, float]:
    delta = float(after) - float(before)
    expected_delta = signed_binance_quantity_delta(leg.side, requested)
    if abs(delta) <= 1e-12 or delta * expected_delta <= 0:
        return {
            "before": float(before),
            "after": float(after),
            "delta": delta,
            "expected_delta": expected_delta,
            "confirmed_fill_lot": 0.0,
        }
    confirmed = min(abs(delta), abs(expected_delta))
    return {
        "before": float(before),
        "after": float(after),
        "delta": delta,
        "expected_delta": expected_delta,
        "confirmed_fill_lot": confirmed,
    }


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
