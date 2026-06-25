from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ...core.contracts import row_get
from ...core.models import BrokerName, OrderSide
from ...reconciliation import (
    BrokerAccountSnapshot,
    BrokerMarginSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
)
from .auth import (
    checked_result_data,
    login_fubon_sdk,
    select_futopt_account,
)
from .parsing import (
    apply_side_sign,
    fubon_first_float,
    fubon_first_text,
    fubon_raw_row,
)


FINAL_ORDER_STATUS_KEYWORDS = (
    "filled",
    "canceled",
    "cancelled",
    "rejected",
    "expired",
    "成交",
    "取消",
    "拒絕",
)
OPEN_ORDER_STATUS_KEYWORDS = (
    "open",
    "pending",
    "new",
    "part",
    "working",
    "委託",
    "等待",
)


class FubonReadOnlyBroker:
    broker = BrokerName.FUBON_QFF

    def __init__(
        self,
        env_path: Path | None = None,
        *,
        sdk: Any | None = None,
        accounts: list[Any] | None = None,
        sdk_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.env_path = env_path
        self.sdk = sdk
        self.accounts = accounts
        self.sdk_factory = sdk_factory
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.account: Any | None = None

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        sdk, account = self._ensure_connected()
        margin_rows = checked_result_data(
            sdk.futopt_accounting.query_margin_equity(account),
            "Fubon query_margin_equity",
        )
        position_rows = checked_result_data(
            sdk.futopt_accounting.query_single_position(account),
            "Fubon query_single_position",
            empty_ok=True,
        )
        order_rows: list[Any] = []
        for market_type in self._futopt_market_types():
            order_rows.extend(
                checked_result_data(
                    sdk.futopt.get_order_results(account, market_type),
                    f"Fubon get_order_results {market_type}",
                    empty_ok=True,
                )
            )

        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id=mask_account(
                row_get(account, "account", "account_no", "id")
            ),
            fetched_at=self.clock(),
            positions=tuple(
                position
                for row in position_rows
                if (position := normalize_fubon_position(row)) is not None
            ),
            open_orders=tuple(
                order
                for row in order_rows
                if (order := normalize_fubon_order(row)) is not None
            ),
            margins=tuple(
                margin
                for row in margin_rows
                if (margin := normalize_fubon_margin(row)) is not None
            ),
            raw={
                "account_type": str(row_get(account, "account_type") or ""),
                "branch_no": mask_account(row_get(account, "branch_no")),
                "margin_rows": len(margin_rows),
                "position_rows": len(position_rows),
                "order_rows": len(order_rows),
            },
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
            self.accounts = login_fubon_sdk(self.sdk, self.env_path)
        self.account = select_futopt_account(self.accounts)
        return self.sdk, self.account

    def _futopt_market_types(self) -> tuple[Any, Any]:
        from fubon_neo.constant import FutOptMarketType

        return FutOptMarketType.Future, FutOptMarketType.FutureNight


def normalize_fubon_position(row: Any) -> BrokerPositionSnapshot | None:
    raw = fubon_raw_row(row)
    symbol = fubon_first_text(
        raw,
        "symbol",
        "code",
        "id",
        "ticker",
        "stock_no",
        "prod_id",
    )
    if not symbol:
        return None
    quantity = fubon_first_float(
        raw,
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
        buy = fubon_first_float(
            raw,
            "buy_lot",
            "buy_qty",
            "buyQuantity",
        )
        sell = fubon_first_float(
            raw,
            "sell_lot",
            "sell_qty",
            "sellQuantity",
        )
        if buy is not None or sell is not None:
            quantity = (buy or 0.0) - (sell or 0.0)
    if quantity is None:
        return None
    side = fubon_first_text(raw, "buy_sell", "bs", "side", "direction")
    signed_quantity = apply_side_sign(quantity, side)
    if signed_quantity == 0:
        return None
    return BrokerPositionSnapshot(
        broker=BrokerName.FUBON_QFF,
        symbol=symbol,
        quantity=signed_quantity,
        raw=raw,
    )


def normalize_fubon_order(row: Any) -> BrokerOrderSnapshot | None:
    raw = fubon_raw_row(row)
    status = (
        fubon_first_text(
            raw,
            "status",
            "order_status",
            "orderStatus",
            "state",
        )
        or ""
    )
    if not is_open_order_status(status):
        return None
    symbol = fubon_first_text(
        raw,
        "symbol",
        "code",
        "id",
        "ticker",
        "stock_no",
        "prod_id",
    )
    order_id = fubon_first_text(
        raw,
        "order_id",
        "orderId",
        "ord_no",
        "seq_no",
        "id",
    )
    quantity = fubon_first_float(raw, "quantity", "qty", "lot", "lots") or 0.0
    return BrokerOrderSnapshot(
        broker=BrokerName.FUBON_QFF,
        order_id=order_id or "UNKNOWN",
        symbol=symbol or "UNKNOWN",
        side=parse_order_side(
            fubon_first_text(raw, "buy_sell", "bs", "side", "direction")
        ),
        quantity=abs(quantity),
        status=status or "open",
        raw=raw,
    )


def normalize_fubon_margin(row: Any) -> BrokerMarginSnapshot | None:
    raw = fubon_raw_row(row)
    return BrokerMarginSnapshot(
        broker=BrokerName.FUBON_QFF,
        currency=fubon_first_text(raw, "currency", "currency_code") or "TWD",
        equity=fubon_first_float(
            raw,
            "equity",
            "account_equity",
            "today_equity",
            "balance",
        ),
        available=fubon_first_float(
            raw,
            "available",
            "available_margin",
            "availableBalance",
        ),
        margin_used=fubon_first_float(
            raw,
            "margin_used",
            "used_margin",
            "initial_margin",
        ),
        raw=raw,
    )


def is_open_order_status(status: str) -> bool:
    normalized = str(status or "").lower()
    if not normalized:
        return False
    if any(keyword in normalized for keyword in FINAL_ORDER_STATUS_KEYWORDS):
        return False
    return any(keyword in normalized for keyword in OPEN_ORDER_STATUS_KEYWORDS)


def parse_order_side(value: Any) -> OrderSide | None:
    text = str(value or "").lower()
    if "buy" in text or text in {"b", "1"} or "買" in text:
        return OrderSide.BUY
    if "sell" in text or text in {"s", "2"} or "賣" in text:
        return OrderSide.SELL
    return None


def mask_account(value: Any, visible: int = 3) -> str:
    text = "" if value is None else str(value)
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * (len(text) - visible) + text[-visible:]


__all__ = [
    "FubonReadOnlyBroker",
    "checked_result_data",
    "normalize_fubon_margin",
    "normalize_fubon_order",
    "normalize_fubon_position",
    "select_futopt_account",
]

