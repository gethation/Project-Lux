from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Callable

from .calendar import in_night_session
from .execution import (
    ExecutionOutcome,
    ExecutionOutcomeStatus,
    order_request_from_execution_leg,
)
from .execution_intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
)
from .live_market_data import (
    load_dotenv,
    parse_optional_float,
    require_env,
    resolve_cert_path,
    row_get,
    row_to_dict,
)
from .models import (
    BrokerName,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)
from .readonly_brokers import checked_result_data, select_futopt_account


FUBON_EXECUTION_SMOKE_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "FUBON_ALLOW_LIVE_ORDER",
    "LUX_FUBON_EXECUTION_SMOKE",
)


@dataclass(frozen=True)
class FubonExecutionPreflight:
    open_orders: tuple[dict[str, Any], ...]
    position_quantity: float


class FubonFutureExecutionAdapter:
    broker = BrokerName.FUBON_QFF

    def __init__(
        self,
        symbol: str,
        env_path: Path | None = None,
        *,
        sdk: Any | None = None,
        accounts: list[Any] | None = None,
        account: Any | None = None,
        sdk_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_poll_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
        unblock: bool = False,
    ) -> None:
        self.symbol = str(symbol).strip()
        self.env_path = env_path
        self.sdk = sdk
        self.accounts = accounts
        self.account = account
        self.sdk_factory = sdk_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.sleep = sleep or time.sleep
        self.max_poll_seconds = float(max_poll_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.unblock = bool(unblock)

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        leg, reject_reason = self._select_leg(plan)
        if reject_reason is not None or leg is None:
            return self._rejected(plan, reject_reason or "invalid_fubon_leg")

        sdk, account = self._ensure_connected()
        try:
            order = self._build_order(plan, leg)
        except Exception as exc:
            return self._failed_from_exception(plan, leg, exc, stage="build_order")

        try:
            place_result = sdk.futopt.place_order(account, order, self.unblock)
            place_rows = checked_result_data(place_result, "Fubon place_order")
        except Exception as exc:
            return self._failed_from_exception(plan, leg, exc, stage="place_order")

        place_row = place_rows[0] if place_rows else {}
        order_id = fubon_order_id(place_row) or "UNKNOWN"
        final_row = self._poll_order_result(
            sdk=sdk,
            account=account,
            plan=plan,
            leg=leg,
            order_id=None if order_id == "UNKNOWN" else order_id,
            fallback_row=place_row,
        )
        return self._outcome_from_order(
            plan,
            leg,
            place_row=place_row,
            final_row=final_row,
        )

    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]:
        sdk, account = self._ensure_connected()
        rows: list[dict[str, Any]] = []
        for market_type in self._futopt_market_types():
            raw_rows = checked_result_data(
                sdk.futopt.get_order_results(account, market_type),
                f"Fubon get_order_results {market_type}",
                empty_ok=True,
            )
            for row in raw_rows:
                raw = safe_jsonable(row_to_dict(row))
                if fubon_symbol(raw) == self.symbol and is_fubon_open_order(raw):
                    rows.append(raw)
        return tuple(rows)

    def fetch_position_quantity(self) -> float:
        sdk, account = self._ensure_connected()
        rows = checked_result_data(
            sdk.futopt_accounting.query_single_position(account),
            "Fubon query_single_position",
            empty_ok=True,
        )
        total = 0.0
        for row in rows:
            raw = safe_jsonable(row_to_dict(row))
            if fubon_symbol(raw) != self.symbol:
                continue
            quantity = fubon_position_quantity(raw)
            if quantity is not None:
                total += quantity
        return total

    def preflight(self) -> FubonExecutionPreflight:
        return FubonExecutionPreflight(
            open_orders=self.fetch_open_orders(),
            position_quantity=self.fetch_position_quantity(),
        )

    def close(self) -> None:
        if self.sdk is not None:
            logout = getattr(self.sdk, "logout", None)
            if callable(logout):
                logout()

    def _ensure_connected(self) -> tuple[Any, Any]:
        if self.sdk is not None and self.account is not None:
            return self.sdk, self.account
        if self.sdk is None:
            from fubon_neo.sdk import FubonSDK

            self.sdk = self.sdk_factory() if self.sdk_factory else FubonSDK()
        if self.accounts is None:
            self.accounts = self._login(self.sdk)
        self.account = select_futopt_account(self.accounts)
        return self.sdk, self.account

    def _login(self, sdk: Any) -> list[Any]:
        load_dotenv(self.env_path)
        personal_id = require_env("FUBON_PERSONAL_ID")
        cert_path = resolve_cert_path(self.env_path)
        cert_password = os.getenv("FUBON_CERT_PASSWORD", "").strip() or None
        api_key = os.getenv("FUBON_API_KEY", "").strip()
        password = os.getenv("FUBON_PASSWORD", "").strip()

        if api_key:
            result = sdk.apikey_login(personal_id, api_key, str(cert_path), cert_password)
        elif password:
            if cert_password:
                result = sdk.login(personal_id, password, str(cert_path), cert_password)
            else:
                result = sdk.login(personal_id, password, str(cert_path))
        else:
            raise RuntimeError("Set FUBON_API_KEY or FUBON_PASSWORD for Fubon login")
        return checked_result_data(result, "Fubon login")

    def _select_leg(
        self,
        plan: PairExecutionPlan,
    ) -> tuple[ExecutionLeg | None, str | None]:
        matches = [leg for leg in plan.legs if leg.broker == BrokerName.FUBON_QFF]
        if len(matches) != 1:
            return None, "plan must contain exactly one Fubon futures leg"
        leg = matches[0]
        if plan.order_type != ExecutionOrderType.MARKET.value:
            return leg, "plan order_type must be market"
        if leg.order_type != ExecutionOrderType.MARKET.value:
            return leg, "Fubon leg order_type must be market"
        if leg.symbol != self.symbol:
            return leg, f"Fubon leg symbol {leg.symbol} does not match {self.symbol}"
        if not is_positive_integer_lot(leg.quantity):
            return leg, "Fubon futures leg quantity must be a positive integer lot"
        if leg.side not in {OrderSide.BUY, OrderSide.SELL}:
            return leg, "Fubon futures leg side must be buy or sell"
        return leg, None

    def _build_order(self, plan: PairExecutionPlan, leg: ExecutionLeg) -> Any:
        from fubon_neo.constant import (
            BSAction,
            FutOptMarketType,
            FutOptOrderType,
            FutOptPriceType,
            TimeInForce,
        )
        from fubon_neo.sdk import FutOptOrder

        side = BSAction.Buy if leg.side == OrderSide.BUY else BSAction.Sell
        order_type = (
            FutOptOrderType.Close
            if plan.plan_type == ExecutionPlanType.EXIT
            else FutOptOrderType.Auto
        )
        market_type = (
            FutOptMarketType.FutureNight
            if in_night_session(plan.timestamp)
            else FutOptMarketType.Future
        )
        return FutOptOrder(
            market_type=market_type,
            price_type=FutOptPriceType.Market,
            time_in_force=TimeInForce.IOC,
            order_type=order_type,
            buy_sell=side,
            symbol=leg.symbol,
            lot=int(float(leg.quantity)),
            price=None,
            user_def="ProjectLux",
        )

    def _futopt_market_types(self) -> tuple[Any, Any]:
        from fubon_neo.constant import FutOptMarketType

        return FutOptMarketType.Future, FutOptMarketType.FutureNight

    def _poll_order_result(
        self,
        *,
        sdk: Any,
        account: Any,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        order_id: str | None,
        fallback_row: Any,
    ) -> dict[str, Any]:
        attempts = max(
            1,
            int(
                self.max_poll_seconds
                / max(self.poll_interval_seconds, 0.001)
            )
            + 1,
        )
        market_type = self._build_order(plan, leg).market_type
        best_row = safe_jsonable(row_to_dict(fallback_row))
        for attempt in range(attempts):
            try:
                rows = checked_result_data(
                    sdk.futopt.get_order_results(account, market_type),
                    f"Fubon get_order_results {market_type}",
                    empty_ok=True,
                )
            except Exception:
                rows = []
            matched = self._match_order_row(rows, order_id, leg)
            if matched is not None:
                best_row = matched
                if is_fubon_final_order(best_row):
                    return best_row
            if attempt < attempts - 1 and self.poll_interval_seconds > 0:
                self.sleep(self.poll_interval_seconds)
        return best_row

    def _match_order_row(
        self,
        rows: list[Any],
        order_id: str | None,
        leg: ExecutionLeg,
    ) -> dict[str, Any] | None:
        candidates = [safe_jsonable(row_to_dict(row)) for row in rows]
        if order_id:
            for row in candidates:
                if fubon_order_id(row) == order_id:
                    return row
        for row in candidates:
            if fubon_symbol(row) == leg.symbol:
                return row
        return None

    def _outcome_from_order(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        *,
        place_row: Any,
        final_row: dict[str, Any],
    ) -> ExecutionOutcome:
        place_raw = safe_jsonable(row_to_dict(place_row))
        order_id = fubon_order_id(final_row) or fubon_order_id(place_raw) or "UNKNOWN"
        requested = int(float(leg.quantity))
        filled_lot = fubon_filled_lot(final_row)
        if filled_lot is None:
            filled_lot = fubon_filled_lot(place_raw)
        filled_lot = float(filled_lot or 0.0)
        average_price = (
            fubon_average_price(final_row)
            or fubon_average_price(place_raw)
            or leg.expected_price
            or leg.price
        )
        status_text = fubon_status_text(final_row) or fubon_status_text(place_raw)
        outcome_status = map_fubon_order_status(
            status_text=status_text,
            requested=float(requested),
            filled=filled_lot,
        )
        order_result = OrderResult(
            order_id=str(order_id),
            request=order_request_from_execution_leg(leg),
            status=order_status_from_outcome(outcome_status),
        )
        fills: tuple[Fill, ...] = ()
        if filled_lot > 0:
            fills = (
                Fill(
                    fill_id=f"FUBON-FILL-{order_id}",
                    order_id=str(order_id),
                    broker=leg.broker,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=filled_lot,
                    price=float(average_price),
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
            message=f"Fubon order {status_text or outcome_status.value}",
            orders=(order_result,),
            fills=fills,
            recommended_state=recommended_state,
            payload={
                "adapter": "fubon_future_execution",
                "symbol": self.symbol,
                "side": leg.side.value,
                "requested_lot": requested,
                "filled_lot": filled_lot,
                "average_price": average_price,
                "status": status_text,
                "seq_no": fubon_first_text(final_row, "seq_no", "seqNo"),
                "order_no": fubon_first_text(
                    final_row,
                    "order_no",
                    "orderNo",
                    "ord_no",
                ),
                "market_type": fubon_first_text(final_row, "market_type", "marketType"),
                "order_type": (
                    "close"
                    if plan.plan_type == ExecutionPlanType.EXIT
                    else "auto"
                ),
                "price_type": "market",
                "time_in_force": "IOC",
                "place_result": safe_jsonable(place_raw),
                "final_order": safe_jsonable(final_row),
            },
        )

    def _rejected(self, plan: PairExecutionPlan, message: str) -> ExecutionOutcome:
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.REJECTED,
            message=message,
            recommended_state=StrategyState.PAUSED,
            payload={"adapter": "fubon_future_execution", "reason": message},
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
            message=f"Fubon {stage} failed: {type(exc).__name__}: {exc}",
            recommended_state=StrategyState.PAUSED,
            payload={
                "adapter": "fubon_future_execution",
                "stage": stage,
                "symbol": self.symbol,
                "side": leg.side.value,
                "quantity": leg.quantity,
                "error_type": type(exc).__name__,
            },
        )


def map_fubon_order_status(
    *,
    status_text: str | None,
    requested: float,
    filled: float,
) -> ExecutionOutcomeStatus:
    normalized = str(status_text or "").strip().lower()
    full_fill = filled >= max(requested - 1e-12, 0.0)
    if full_fill:
        return ExecutionOutcomeStatus.FILLED
    if filled > 0.0:
        return ExecutionOutcomeStatus.PARTIAL_FILL
    if any(keyword in normalized for keyword in ("cancel", "reject", "fail", "expire")):
        return ExecutionOutcomeStatus.FAILED
    if any(keyword in normalized for keyword in ("filled", "match")) and full_fill:
        return ExecutionOutcomeStatus.FILLED
    return ExecutionOutcomeStatus.UNKNOWN


def order_status_from_outcome(status: ExecutionOutcomeStatus) -> OrderStatus:
    if status == ExecutionOutcomeStatus.FILLED:
        return OrderStatus.FILLED
    if status == ExecutionOutcomeStatus.FAILED:
        return OrderStatus.CANCELED
    return OrderStatus.OPEN


def fubon_order_id(row: Any) -> str | None:
    raw = row_to_dict(row)
    return fubon_first_text(
        raw,
        "order_id",
        "orderId",
        "ord_no",
        "ordNo",
        "order_no",
        "orderNo",
        "seq_no",
        "seqNo",
        "id",
    )


def fubon_symbol(row: Any) -> str | None:
    raw = row_to_dict(row)
    return fubon_first_text(raw, "symbol", "code", "id", "ticker", "stock_no", "prod_id")


def fubon_status_text(row: Any) -> str:
    raw = row_to_dict(row)
    return (
        fubon_first_text(raw, "status", "order_status", "orderStatus", "state")
        or ""
    )


def fubon_filled_lot(row: Any) -> float | None:
    raw = row_to_dict(row)
    return fubon_first_float(
        raw,
        "filled_lot",
        "filledLot",
        "filled_qty",
        "filledQty",
        "filled_quantity",
        "filledQuantity",
        "match_lot",
        "matchLot",
        "deal_lot",
        "dealLot",
        "filled",
        "executedQty",
    )


def fubon_average_price(row: Any) -> float | None:
    raw = row_to_dict(row)
    return fubon_first_float(
        raw,
        "average_price",
        "averagePrice",
        "avg_price",
        "avgPrice",
        "filled_price",
        "filledPrice",
        "match_price",
        "matchPrice",
        "deal_price",
        "dealPrice",
        "price",
    )


def fubon_position_quantity(row: dict[str, Any]) -> float | None:
    quantity = fubon_first_float(
        row,
        "net_quantity",
        "net_qty",
        "netPosition",
        "position",
        "quantity",
        "qty",
        "lot",
        "lots",
    )
    if quantity is None:
        buy = fubon_first_float(row, "buy_lot", "buy_qty", "buyQuantity")
        sell = fubon_first_float(row, "sell_lot", "sell_qty", "sellQuantity")
        if buy is not None or sell is not None:
            quantity = (buy or 0.0) - (sell or 0.0)
    if quantity is None:
        return None
    return apply_side_sign(quantity, fubon_first_text(row, "buy_sell", "bs", "side"))


def is_fubon_open_order(row: dict[str, Any]) -> bool:
    status = fubon_status_text(row)
    if is_fubon_final_order(row):
        return False
    return bool(status) or fubon_filled_lot(row) is not None


def is_fubon_final_order(row: dict[str, Any]) -> bool:
    status = fubon_status_text(row).lower()
    filled = fubon_filled_lot(row) or 0.0
    requested = fubon_first_float(row, "lot", "lots", "quantity", "qty") or 0.0
    if requested > 0 and filled >= requested:
        return True
    return any(
        keyword in status
        for keyword in (
            "filled",
            "cancel",
            "reject",
            "fail",
            "expire",
        )
    )


def fubon_first_text(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = row_get(row, name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def fubon_first_float(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = parse_optional_float(row_get(row, name))
        if value is not None:
            return value
    return None


def apply_side_sign(quantity: float, side: str | None) -> float:
    text = str(side or "").strip().lower()
    if "sell" in text or "short" in text or text in {"s", "2"}:
        return -abs(quantity)
    if "buy" in text or "long" in text or text in {"b", "1"}:
        return abs(quantity)
    return quantity


def is_positive_integer_lot(value: float | int | None) -> bool:
    if value is None:
        return False
    numeric = float(value)
    return isfinite(numeric) and numeric > 0 and numeric.is_integer()


def fubon_smoke_env_gates_open() -> dict[str, bool]:
    return {
        name: os.getenv(name, "").strip() == "1"
        for name in FUBON_EXECUTION_SMOKE_ENV_GATES
    }


def safe_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): safe_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return safe_jsonable(getattr(value, "value"))
    return repr(value)
