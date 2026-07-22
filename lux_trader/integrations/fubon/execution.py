from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Any, Callable

from ...core.calendar import in_night_session
from ...core.contracts import row_to_dict
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
    ExecutionPreflight,
    order_request_from_execution_leg,
)
from ...execution.intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
)
from .auth import checked_result_data, login_fubon_sdk, select_futopt_account
from .contracts import (
    FubonContractIdentity,
    fubon_contract_month,
    fubon_lot_matches,
    fubon_side_matches,
    fubon_symbol,
)
from .fill_listener import FillWaiter, FubonFillReportListener
from .parsing import (
    apply_side_sign,
    fubon_first_float,
    fubon_first_text,
    fubon_raw_row,
    safe_jsonable,
)

# Official futures order status enum (TradeAPI trading-future Enumerations):
# 0=Reservation, 4=InQueue(re-query), 9=TimeOut(re-query), 10=New Order,
# 30=Cancel, 39=Cancel failed, 50=Fully filled, 90=Failed. There is no
# dedicated partial-fill status — partial fills accumulate via filled lots.
# 80/91/98/99 are kept defensively from observed legacy responses.
FUBON_FAILED_STATUS_CODES = {"30", "80", "90", "91", "98", "99"}
FUBON_WORKING_STATUS_CODES = {"0", "4", "10"}
FUBON_REQUERY_STATUS_CODES = {"9"}
FUBON_EXECUTION_SMOKE_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "FUBON_ALLOW_LIVE_ORDER",
    "LUX_FUBON_EXECUTION_SMOKE",
)
FUBON_MANUAL_CLOSE_ENV_GATES = (
    "PROJECT_LUX_ALLOW_LIVE_ORDER",
    "FUBON_ALLOW_LIVE_ORDER",
    "LUX_FUBON_MANUAL_CLOSE",
)


FubonExecutionPreflight = ExecutionPreflight


@dataclass(frozen=True)
class FubonOrderPollResult:
    final_row: dict[str, Any]
    errors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class FubonFillWaitResult:
    """Merged callback + polling confirmation evidence for one order."""

    final_row: dict[str, Any]
    poll_errors: tuple[dict[str, Any], ...] = ()
    callback_filled_lot: float = 0.0
    callback_avg_price: float | None = None
    fill_events: tuple[dict[str, Any], ...] = ()
    order_reports: tuple[dict[str, Any], ...] = ()
    stream_unreliable: bool = False
    wait_elapsed_seconds: float | None = None
    matched_order_result: bool = False
    stale_order_results: tuple[dict[str, Any], ...] = ()


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
        callback_grace_seconds: float = 0.5,
        order_match_time_window_seconds: float | None = 120.0,
        unblock: bool = False,
    ) -> None:
        self.symbol = str(symbol).strip()
        self.env_path = env_path
        self.sdk = sdk
        self.accounts = accounts
        self.account = account
        self.sdk_factory = sdk_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.identity = FubonContractIdentity.from_symbol(
            self.symbol,
            reference_date=self.clock().date(),
        )
        self.sleep = sleep or time.sleep
        self.max_poll_seconds = float(max_poll_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.callback_grace_seconds = float(callback_grace_seconds)
        self.order_match_time_window_seconds = (
            float(order_match_time_window_seconds)
            if order_match_time_window_seconds is not None
            else None
        )
        self.unblock = bool(unblock)
        self.fill_listener: FubonFillReportListener | None = None
        self.session_generation = 0
        self.last_login_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_invalid_reason: str | None = None
        self.relogin_count = 0
        self.session_event_callback_attached = False

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        leg, reject_reason = self._select_leg(plan)
        if reject_reason is not None or leg is None:
            return self._rejected(plan, reject_reason or "invalid_fubon_leg")

        try:
            position_before = self.fetch_position_quantity()
            position_errors: list[dict[str, Any]] = []
        except Exception as exc:
            if not is_fubon_session_invalid_error(exc):
                return self._failed_from_exception(
                    plan, leg, exc, stage="pre_submit_health"
                )
            self._reset_session(str(exc))
            try:
                position_before = self.fetch_position_quantity()
                position_errors = []
            except Exception as retry_exc:
                return self._failed_from_exception(
                    plan, leg, retry_exc, stage="pre_submit_reauth"
                )
        sdk, account = self._ensure_connected()
        try:
            order = self._build_order(plan, leg)
        except Exception as exc:
            return self._failed_from_exception(plan, leg, exc, stage="build_order")

        # Arm the fill waiter BEFORE placing: a fill callback can arrive before
        # place_order returns; the listener buffers it until keys are known.
        waiter = (
            self.fill_listener.register_waiter()
            if self.fill_listener is not None and self.fill_listener.active
            else None
        )
        try:
            try:
                submit_started_at = self.clock()
                place_result = sdk.futopt.place_order(account, order, self.unblock)
                submit_finished_at = self.clock()
            except Exception as exc:
                return self._unknown_from_exception(
                    plan, leg, exc, stage="place_order"
                )
            try:
                place_rows = checked_result_data(place_result, "Fubon place_order")
            except Exception as exc:
                if is_fubon_session_invalid_error(exc):
                    return self._unknown_from_exception(
                        plan, leg, exc, stage="place_order_result"
                    )
                return self._failed_from_exception(
                    plan, leg, exc, stage="place_order_rejected"
                )

            place_row = place_rows[0] if place_rows else {}
            if waiter is not None:
                waiter.set_keys(
                    fubon_seq_no(place_row),
                    fubon_order_id(place_row),
                )
            return self._confirm_and_build_outcome(
                sdk=sdk,
                account=account,
                plan=plan,
                leg=leg,
                place_row=place_row,
                waiter=waiter,
                position_before=position_before,
                position_errors=position_errors,
                submit_started_at=submit_started_at,
                submit_finished_at=submit_finished_at,
            )
        finally:
            if waiter is not None:
                waiter.close()

    def _confirm_and_build_outcome(
        self,
        *,
        sdk: Any,
        account: Any,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        place_row: Any,
        waiter: "FillWaiter | None",
        position_before: float | None,
        position_errors: list[dict[str, Any]],
        submit_started_at: datetime,
        submit_finished_at: datetime,
    ) -> ExecutionOutcome:
        order_keys = tuple(
            dict.fromkeys(
                key
                for key in (
                    fubon_order_id(place_row),
                    fubon_seq_no(place_row),
                )
                if key
            )
        )
        wait_result = self._await_order_completion(
            sdk=sdk,
            account=account,
            plan=plan,
            leg=leg,
            order_keys=order_keys,
            place_row=place_row,
            waiter=waiter,
            submit_started_at=submit_started_at,
        )
        requested_lot = int(float(leg.quantity))
        callback_confirmed_full = (
            wait_result.callback_filled_lot >= float(requested_lot) - 1e-12
        )
        position_delta = None
        # A polled order row is supporting evidence only.  Whenever the
        # callback did not confirm the complete fill, require an independent
        # position delta even if get_order_results claims the order is filled.
        # This prevents a stale terminal row from becoming the trade truth.
        if position_before is not None and not callback_confirmed_full:
            position_after, position_after_errors = self._safe_fetch_position_quantity(
                stage="after_order"
            )
            position_errors.extend(position_after_errors)
            if position_after is not None:
                position_delta = fubon_position_delta_confirmation(
                    leg=leg,
                    requested=float(requested_lot),
                    before=position_before,
                    after=position_after,
                )
        return self._outcome_from_order(
            plan,
            leg,
            place_row=place_row,
            final_row=wait_result.final_row,
            poll_errors=wait_result.poll_errors,
            position_before=position_before,
            position_delta=position_delta,
            position_errors=tuple(position_errors),
            submit_started_at=submit_started_at,
            submit_finished_at=submit_finished_at,
            callback_filled_lot=wait_result.callback_filled_lot,
            callback_avg_price=wait_result.callback_avg_price,
            fill_events=wait_result.fill_events,
            order_reports=wait_result.order_reports,
            stream_unreliable=wait_result.stream_unreliable,
            wait_elapsed_seconds=wait_result.wait_elapsed_seconds,
            matched_order_result=wait_result.matched_order_result,
            stale_order_results=wait_result.stale_order_results,
        )

    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]:
        sdk, account = self._ensure_connected()
        rows: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for market_type in self._futopt_market_types():
            raw_rows = checked_result_data(
                sdk.futopt.get_order_results(account, market_type),
                f"Fubon get_order_results {market_type}",
                empty_ok=True,
            )
            for row in raw_rows:
                raw = fubon_raw_row(row)
                if self.identity.matches(raw) and is_fubon_open_order(raw):
                    key = fubon_record_key(raw)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(raw)
        return tuple(rows)

    def fetch_order_records(self) -> tuple[dict[str, Any], ...]:
        sdk, account = self._ensure_connected()
        rows: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for market_type in self._futopt_market_types():
            raw_rows = checked_result_data(
                sdk.futopt.get_order_results(account, market_type),
                f"Fubon get_order_results {market_type}",
                empty_ok=True,
            )
            for row in raw_rows:
                raw = fubon_raw_row(row)
                if self.identity.matches(raw):
                    key = fubon_record_key(raw)
                    if key in seen:
                        continue
                    seen.add(key)
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
            raw = fubon_raw_row(row)
            if not self.identity.matches(raw):
                continue
            quantity = fubon_position_quantity(raw)
            if quantity is not None:
                total += quantity
        self.last_success_at = self.clock()
        return total

    def preflight(self) -> FubonExecutionPreflight:
        return FubonExecutionPreflight(
            open_orders=self.fetch_open_orders(),
            position_quantity=self.fetch_position_quantity(),
        )

    def session_health(self) -> dict[str, Any]:
        return {
            "role": "trading",
            "generation": self.session_generation,
            "status": "invalid" if self.last_invalid_reason else "ready",
            "last_login_at": self.last_login_at,
            "last_success_at": self.last_success_at,
            "invalid_reason": self.last_invalid_reason,
            "relogin_count": self.relogin_count,
        }

    def close(self) -> None:
        if self.sdk is not None:
            logout = getattr(self.sdk, "logout", None)
            if callable(logout):
                logout()

    def _reset_session(self, reason: str) -> None:
        if self.fill_listener is not None:
            self.fill_listener.close()
            self.fill_listener = None
        if self.sdk is not None:
            logout = getattr(self.sdk, "logout", None)
            if callable(logout):
                try:
                    logout()
                except Exception:
                    pass
        self.sdk = None
        self.accounts = None
        self.account = None
        self.session_event_callback_attached = False
        self.last_invalid_reason = reason
        self.relogin_count += 1

    def _ensure_connected(self) -> tuple[Any, Any]:
        if self.sdk is not None and self.account is not None:
            self._ensure_fill_listener()
            return self.sdk, self.account
        if self.sdk is None:
            from fubon_neo.sdk import FubonSDK

            self.sdk = self.sdk_factory() if self.sdk_factory else FubonSDK()
        if self.accounts is None:
            self.accounts = self._login(self.sdk)
        self.account = select_futopt_account(self.accounts)
        self.session_generation += 1
        self.last_login_at = self.clock()
        self.last_invalid_reason = None
        self._ensure_fill_listener()
        return self.sdk, self.account

    def _handle_session_event(self, *args: Any, **kwargs: Any) -> None:
        text = " ".join(str(item) for item in args)
        if kwargs:
            text = f"{text} {kwargs}"
        normalized = text.lower()
        if any(code in normalized for code in ("300", "301", "302", "304")):
            self.last_invalid_reason = f"Fubon session event {text.strip()}"

    def _ensure_fill_listener(self) -> None:
        if self.fill_listener is None and self.sdk is not None:
            self.fill_listener = FubonFillReportListener.attach(
                self.sdk,
                event_observer=self._handle_session_event,
            )
            self.session_event_callback_attached = True

    def _login(self, sdk: Any) -> list[Any]:
        return login_fubon_sdk(
            sdk,
            self.env_path,
            api_key_env="FUBON_TRADING_API_KEY",
        )

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

    def _await_order_completion(
        self,
        *,
        sdk: Any,
        account: Any,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        order_keys: tuple[str, ...],
        place_row: Any,
        waiter: "FillWaiter | None",
        submit_started_at: datetime,
    ) -> FubonFillWaitResult:
        """Callback-first fill confirmation with polling as the backup channel.

        Returns as soon as one of these holds: the fill callbacks accumulated
        the requested lots; an order report / polled row reached a terminal
        state (50 filled, 30 cancel, 90 failed, ...); or the bounded timeout
        elapsed. Official status 9 (TimeOut) triggers an immediate re-query.
        """
        attempts = max(
            1,
            int(
                self.max_poll_seconds
                / max(self.poll_interval_seconds, 0.001)
            )
            + 1,
        )
        requested = float(int(float(leg.quantity)))
        market_type = self._build_order(plan, leg).market_type
        best_row = fubon_raw_row(place_row)
        errors: list[dict[str, Any]] = []
        stale_order_results: list[dict[str, Any]] = []
        matched_order_result = False
        started_at = time.monotonic()

        def waiter_result(row: dict[str, Any]) -> FubonFillWaitResult:
            return FubonFillWaitResult(
                final_row=row,
                poll_errors=tuple(errors),
                callback_filled_lot=(
                    float(waiter.filled_lots) if waiter is not None else 0.0
                ),
                callback_avg_price=(
                    waiter.average_fill_price() if waiter is not None else None
                ),
                fill_events=(
                    tuple(waiter.fill_events) if waiter is not None else ()
                ),
                order_reports=(
                    tuple(waiter.order_reports) if waiter is not None else ()
                ),
                stream_unreliable=(
                    self.fill_listener.stream_unreliable
                    if self.fill_listener is not None
                    else False
                ),
                wait_elapsed_seconds=time.monotonic() - started_at,
                matched_order_result=matched_order_result,
                stale_order_results=tuple(stale_order_results),
            )

        # Give the primary callback channel one short exclusive opportunity
        # before issuing the first history query.  Real fills commonly arrive
        # within milliseconds of place_order returning.
        if waiter is not None and self.callback_grace_seconds > 0:
            if waiter.filled_lots < requested - 1e-12:
                waiter.wait(self.callback_grace_seconds)
            if waiter.filled_lots >= requested - 1e-12:
                return waiter_result(best_row)

        attempt = 0
        while attempt < attempts:
            attempt += 1
            if waiter is not None:
                if waiter.filled_lots >= requested - 1e-12:
                    return waiter_result(best_row)
                terminal = normalized_fubon_status(waiter.terminal_status)
                if terminal in FUBON_FAILED_STATUS_CODES:
                    merged = dict(best_row)
                    merged["status"] = waiter.terminal_status
                    return waiter_result(merged)
            try:
                rows = checked_result_data(
                    sdk.futopt.get_order_results(account, market_type),
                    f"Fubon get_order_results {market_type}",
                    empty_ok=True,
                )
            except Exception as exc:
                errors.append(
                    {
                        "attempt": attempt,
                        "stage": "get_order_results",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                rows = []
            matched = self._match_order_row(
                rows,
                order_keys,
                leg,
                submit_started_at=submit_started_at,
            )
            if matched is not None:
                best_row = matched
                matched_order_result = True
                if is_fubon_final_order(best_row):
                    return waiter_result(best_row)
                status = normalized_fubon_status(fubon_status_text(best_row))
                if status in FUBON_REQUERY_STATUS_CODES:
                    # Official status 9 = TimeOut: the broker asks clients to
                    # re-query; do so immediately without burning the interval.
                    continue
            elif order_keys:
                for row in rows:
                    raw = fubon_raw_row(row)
                    if not self.identity.matches(
                        raw,
                        side=leg.side,
                        lot=leg.quantity,
                    ):
                        continue
                    safe = safe_jsonable(raw) or {}
                    if safe not in stale_order_results:
                        stale_order_results.append(safe)
            if attempt < attempts and self.poll_interval_seconds > 0:
                if waiter is not None:
                    waiter.wait(self.poll_interval_seconds)
                else:
                    self.sleep(self.poll_interval_seconds)
        return waiter_result(best_row)

    def _match_order_row(
        self,
        rows: list[Any],
        order_keys: tuple[str, ...],
        leg: ExecutionLeg,
        *,
        submit_started_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        candidates = [fubon_raw_row(row) for row in rows]
        if order_keys:
            known_keys = set(order_keys)
            for row in candidates:
                candidate_keys = {
                    key
                    for key in (fubon_order_id(row), fubon_seq_no(row))
                    if key
                }
                if known_keys.intersection(candidate_keys):
                    observed_at = fubon_order_timestamp(
                        row,
                        timezone=(
                            submit_started_at.tzinfo
                            if submit_started_at is not None
                            else None
                        ),
                    )
                    if (
                        observed_at is not None
                        and submit_started_at is not None
                        and self.order_match_time_window_seconds is not None
                        and abs(
                            (observed_at - submit_started_at).total_seconds()
                        )
                        > self.order_match_time_window_seconds
                    ):
                        continue
                    return row
            # ``place_order`` gave us a broker identifier, so a row with only
            # the same contract/side/lot is not evidence that it is this
            # order.  The broker history can still contain an older terminal
            # order with the same shape while the new IOC order has not become
            # queryable yet.  Falling through here would treat that stale row
            # as the new fill and can reuse its order/fill IDs in the ledger.
            return None
        for row in candidates:
            if self.identity.matches(row, side=leg.side, lot=leg.quantity):
                return row
        for row in candidates:
            if self.identity.matches(row):
                return row
        return None

    def _outcome_from_order(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        *,
        place_row: Any,
        final_row: dict[str, Any],
        poll_errors: tuple[dict[str, Any], ...] = (),
        position_before: float | None = None,
        position_delta: dict[str, float] | None = None,
        position_errors: tuple[dict[str, Any], ...] = (),
        submit_started_at: datetime | None = None,
        submit_finished_at: datetime | None = None,
        callback_filled_lot: float = 0.0,
        callback_avg_price: float | None = None,
        fill_events: tuple[dict[str, Any], ...] = (),
        order_reports: tuple[dict[str, Any], ...] = (),
        stream_unreliable: bool = False,
        wait_elapsed_seconds: float | None = None,
        matched_order_result: bool = False,
        stale_order_results: tuple[dict[str, Any], ...] = (),
    ) -> ExecutionOutcome:
        place_raw = fubon_raw_row(place_row)
        attempt_id = fubon_attempt_id(plan)
        order_id = attempt_id
        requested = int(float(leg.quantity))
        filled_lot = float(callback_filled_lot or 0.0)
        fill_source = "filled_callback" if filled_lot > 0 else None
        position_delta_fill_lot = 0.0
        if position_delta is not None:
            position_delta_fill_lot = float(
                position_delta.get("confirmed_fill_lot") or 0.0
            )
        if (
            callback_filled_lot < float(requested) - 1e-12
            and position_delta_fill_lot > filled_lot + 1e-12
        ):
            filled_lot = position_delta_fill_lot
            fill_source = "position_delta"
        exact_query_price = (
            fubon_average_price(final_row)
            or fubon_filled_money_price(final_row, filled_lot)
            if matched_order_result
            else None
        )
        average_price = (
            callback_avg_price
            or exact_query_price
            or leg.expected_price
            or leg.price
        )
        price_quality = (
            "actual"
            if callback_avg_price is not None or exact_query_price is not None
            else "estimated"
        )
        final_status_text = fubon_status_text(final_row)
        status_text = (
            final_status_text
            if (
                matched_order_result
                or normalized_fubon_status(final_status_text)
                in FUBON_FAILED_STATUS_CODES
            )
            else ""
        ) or fubon_status_text(place_raw)
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
            message=(
                f"Fubon order {status_text or outcome_status.value}"
                + (
                    " position_delta_confirmed"
                    if fill_source == "position_delta"
                    else ""
                )
            ),
            orders=(order_result,),
            fills=fills,
            recommended_state=recommended_state,
            payload={
                "adapter": "fubon_future_execution",
                "attempt_id": attempt_id,
                "symbol": self.symbol,
                "side": leg.side.value,
                "submit_started_at": submit_started_at,
                "submit_finished_at": submit_finished_at,
                "requested_lot": requested,
                "filled_lot": filled_lot,
                "fill_source": fill_source,
                "confirmation_source": fill_source,
                "average_price": average_price,
                "fill_price_quality": price_quality,
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
                "poll_errors": tuple(poll_errors),
                "position_before": position_before,
                "position_delta": position_delta,
                "position_errors": tuple(position_errors),
                "place_result": safe_jsonable(place_raw),
                "final_order": safe_jsonable(final_row),
                "matched_order_result": matched_order_result,
                "stale_order_results": tuple(stale_order_results),
                "fill_events": tuple(fill_events),
                "order_reports": tuple(order_reports),
                "callback_filled_lot": callback_filled_lot,
                "callback_stream_unreliable": stream_unreliable,
                "wait_elapsed_seconds": wait_elapsed_seconds,
                "callback_errors": tuple(
                    self.fill_listener.callback_errors
                    if self.fill_listener is not None
                    else ()
                ),
            },
        )

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

    def _unknown_from_exception(
        self,
        plan: PairExecutionPlan,
        leg: ExecutionLeg,
        exc: Exception,
        *,
        stage: str,
    ) -> ExecutionOutcome:
        if is_fubon_session_invalid_error(exc):
            self.last_invalid_reason = str(exc)
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=self.clock(),
            status=ExecutionOutcomeStatus.UNKNOWN,
            message=(
                f"Fubon {stage} outcome unknown: {type(exc).__name__}: {exc}"
            ),
            recommended_state=StrategyState.PAUSED,
            payload={
                "adapter": "fubon_future_execution",
                "stage": stage,
                "submission_started": True,
                "do_not_retry": True,
                "symbol": self.symbol,
                "side": leg.side.value,
                "quantity": leg.quantity,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )


def normalized_fubon_status(status_text: str | None) -> str:
    return str(status_text or "").strip().lower()


def is_fubon_session_invalid_error(exc: BaseException | str) -> bool:
    text = str(exc).strip().lower()
    return (
        "not login" in text
        or "not logged" in text
        or "event 300" in text
        or "event 301" in text
        or "event 302" in text
        or "event 304" in text
    )


def map_fubon_order_status(
    *,
    status_text: str | None,
    requested: float,
    filled: float,
) -> ExecutionOutcomeStatus:
    normalized = normalized_fubon_status(status_text)
    full_fill = filled >= max(requested - 1e-12, 0.0)
    if full_fill:
        return ExecutionOutcomeStatus.FILLED
    if filled > 0.0:
        return ExecutionOutcomeStatus.PARTIAL_FILL
    if normalized in FUBON_FAILED_STATUS_CODES:
        return ExecutionOutcomeStatus.FAILED
    if any(keyword in normalized for keyword in ("cancel", "reject", "fail", "expire")):
        return ExecutionOutcomeStatus.FAILED
    if any(keyword in normalized for keyword in ("filled", "match")) and full_fill:
        return ExecutionOutcomeStatus.FILLED
    return ExecutionOutcomeStatus.UNKNOWN


def order_row_is_filled(
    final_row: Any,
    place_row: Any,
    *,
    requested: float,
) -> bool:
    filled_lot = fubon_filled_lot(final_row)
    if filled_lot is None:
        filled_lot = fubon_filled_lot(place_row)
    return (
        map_fubon_order_status(
            status_text=fubon_status_text(final_row) or fubon_status_text(place_row),
            requested=float(requested),
            filled=float(filled_lot or 0.0),
        )
        == ExecutionOutcomeStatus.FILLED
    )


def fubon_position_delta_confirmation(
    *,
    leg: ExecutionLeg,
    requested: float,
    before: float,
    after: float,
) -> dict[str, float] | None:
    delta = float(after) - float(before)
    expected_delta = signed_fubon_lot_delta(leg.side, requested)
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


def signed_fubon_lot_delta(side: OrderSide, quantity: float) -> float:
    return float(quantity) if side == OrderSide.BUY else -float(quantity)


def fubon_attempt_id(plan: PairExecutionPlan) -> str:
    """Return the canonical local identity for one Fubon execution attempt.

    Broker identifiers are aliases only: Fubon history/callback rows have
    proven that they can be stale or repeated.  The leg plan id is already
    unique for primary and emergency-close attempts and is deterministic
    across persistence/replay.
    """

    return f"LUX-FUBON-{plan.plan_id}"


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


def fubon_record_key(row: Any) -> tuple[Any, ...]:
    raw = row_to_dict(row)
    return (
        fubon_order_id(raw),
        fubon_seq_no(raw),
        fubon_symbol(raw),
        fubon_contract_month(raw),
        fubon_first_text(raw, "buy_sell", "buySell", "bs", "side"),
        fubon_first_float(raw, "lot", "lots", "quantity", "qty"),
    )


def fubon_seq_no(row: Any) -> str | None:
    raw = row_to_dict(row)
    return fubon_first_text(raw, "seq_no", "seqNo")



def fubon_status_text(row: Any) -> str:
    raw = row_to_dict(row)
    return (
        fubon_first_text(raw, "status", "order_status", "orderStatus", "state")
        or ""
    )


def fubon_order_timestamp(
    row: Any,
    *,
    timezone: Any = None,
) -> datetime | None:
    raw = row_to_dict(row)
    date_text = fubon_first_text(raw, "date", "order_date", "orderDate")
    time_text = fubon_first_text(
        raw,
        "last_time",
        "lastTime",
        "time",
        "order_time",
        "orderTime",
    )
    if not date_text or not time_text:
        return None
    try:
        parsed = datetime.fromisoformat(
            f"{date_text.replace('/', '-')}T{time_text}"
        )
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone) if timezone is not None else parsed



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


def fubon_filled_money_price(row: Any, filled_lot: float) -> float | None:
    if filled_lot <= 0:
        return None
    raw = row_to_dict(row)
    money = fubon_first_float(raw, "filled_money", "filledMoney")
    if money is None:
        return None
    return money / filled_lot


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
        "orig_lots",
        "origLots",
        "orig_lot",
        "origLot",
        "original_lots",
        "originalLots",
        "tradable_lots",
        "tradableLots",
        "tradable_lot",
        "tradableLot",
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
    if status in FUBON_FAILED_STATUS_CODES:
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


def fubon_manual_close_env_gates_open() -> dict[str, bool]:
    return {
        name: os.getenv(name, "").strip() == "1"
        for name in FUBON_MANUAL_CLOSE_ENV_GATES
    }


