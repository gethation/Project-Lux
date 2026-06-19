from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .live_market_data import (
    first_float,
    load_dotenv,
    parse_optional_float,
    require_env,
    resolve_cert_path,
    row_get,
    row_to_dict,
)
from .models import BrokerName, OrderSide
from .reconciliation import (
    BrokerAccountSnapshot,
    BrokerMarginSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
)


FINAL_ORDER_STATUS_KEYWORDS = (
    "filled",
    "canceled",
    "cancelled",
    "rejected",
    "expired",
    "成交",
    "取消",
    "刪單",
    "失敗",
)
OPEN_ORDER_STATUS_KEYWORDS = (
    "open",
    "pending",
    "new",
    "part",
    "working",
    "委託",
    "未成交",
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
            account_id=mask_account(row_get(account, "account", "account_no", "id")),
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

    def _futopt_market_types(self) -> tuple[Any, Any]:
        from fubon_neo.constant import FutOptMarketType

        return FutOptMarketType.Future, FutOptMarketType.FutureNight


class BinanceReadOnlyBroker:
    broker = BrokerName.BINANCE_TSM

    def __init__(
        self,
        symbol: str,
        env_path: Path | None = None,
        *,
        exchange: Any | None = None,
        exchange_factory: Callable[[dict[str, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.symbol = symbol
        self.env_path = env_path
        self.exchange = exchange
        self.exchange_factory = exchange_factory
        self.clock = clock or (lambda: datetime.now().astimezone())

    def fetch_snapshot(self) -> BrokerAccountSnapshot:
        exchange = self._ensure_exchange()
        balance = exchange.fetch_balance()
        positions = exchange.fetch_positions([self.symbol])
        open_orders = exchange.fetch_open_orders(self.symbol)
        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id="BINANCE_USDM",
            fetched_at=self.clock(),
            positions=tuple(
                position
                for row in positions
                if (position := normalize_binance_position(row, self.symbol)) is not None
            ),
            open_orders=tuple(
                order
                for row in open_orders
                if (order := normalize_binance_order(row)) is not None
            ),
            margins=tuple(normalize_binance_margins(balance)),
            raw={
                "exchange": "binanceusdm",
                "symbol": self.symbol,
                "position_rows": len(positions or []),
                "open_order_rows": len(open_orders or []),
            },
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
        self.exchange.load_markets()
        return self.exchange


def checked_result_data(
    result: Any,
    label: str,
    *,
    empty_ok: bool = False,
) -> list[Any]:
    if not bool(getattr(result, "is_success", True)):
        message = str(getattr(result, "message", "") or "")
        if empty_ok and is_empty_result_message(message):
            return []
        raise RuntimeError(f"{label} failed: {message}")
    data = getattr(result, "data", result)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def is_empty_result_message(message: str) -> bool:
    normalized = message.strip().lower()
    return (
        "查無任何資料" in normalized
        or "查無資料" in normalized
        or "no data" in normalized
        or "not found" in normalized
    )


def select_futopt_account(accounts: list[Any]) -> Any:
    if not accounts:
        raise RuntimeError("Fubon login returned no accounts")
    for account in accounts:
        if str(row_get(account, "account_type") or "").lower() == "futopt":
            return account
    raise RuntimeError("Fubon login returned no futopt account")


def normalize_fubon_position(row: Any) -> BrokerPositionSnapshot | None:
    raw = safe_jsonable(row_to_dict(row))
    symbol = first_text(raw, "symbol", "code", "id", "ticker", "stock_no", "prod_id")
    if not symbol:
        return None
    quantity = first_float(
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
        buy = first_float(raw, "buy_lot", "buy_qty", "buyQuantity")
        sell = first_float(raw, "sell_lot", "sell_qty", "sellQuantity")
        if buy is not None or sell is not None:
            quantity = (buy or 0.0) - (sell or 0.0)
    if quantity is None:
        return None
    side = first_text(raw, "buy_sell", "bs", "side", "direction")
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
    raw = safe_jsonable(row_to_dict(row))
    status = first_text(raw, "status", "order_status", "orderStatus", "state") or ""
    if not is_open_order_status(status):
        return None
    symbol = first_text(raw, "symbol", "code", "id", "ticker", "stock_no", "prod_id")
    order_id = first_text(raw, "order_id", "orderId", "ord_no", "seq_no", "id")
    quantity = first_float(raw, "quantity", "qty", "lot", "lots") or 0.0
    return BrokerOrderSnapshot(
        broker=BrokerName.FUBON_QFF,
        order_id=order_id or "UNKNOWN",
        symbol=symbol or "UNKNOWN",
        side=parse_order_side(first_text(raw, "buy_sell", "bs", "side", "direction")),
        quantity=abs(quantity),
        status=status or "open",
        raw=raw,
    )


def normalize_fubon_margin(row: Any) -> BrokerMarginSnapshot | None:
    raw = safe_jsonable(row_to_dict(row))
    return BrokerMarginSnapshot(
        broker=BrokerName.FUBON_QFF,
        currency=first_text(raw, "currency", "currency_code") or "TWD",
        equity=first_float(raw, "equity", "account_equity", "balance"),
        available=first_float(raw, "available", "available_margin", "availableBalance"),
        margin_used=first_float(raw, "margin_used", "used_margin", "initial_margin"),
        raw=raw,
    )


def normalize_binance_position(
    row: dict[str, Any],
    expected_symbol: str,
) -> BrokerPositionSnapshot | None:
    raw = safe_jsonable(row)
    symbol = str(row.get("symbol") or raw.get("symbol") or "")
    if symbol != expected_symbol:
        return None
    info = row.get("info") if isinstance(row.get("info"), dict) else {}
    quantity = parse_optional_float(info.get("positionAmt")) if info else None
    if quantity is None:
        quantity = parse_optional_float(row.get("contracts"))
        side = str(row.get("side") or "").lower()
        if quantity is not None and side == "short":
            quantity = -abs(quantity)
    if quantity is None or quantity == 0:
        return None
    return BrokerPositionSnapshot(
        broker=BrokerName.BINANCE_TSM,
        symbol=symbol,
        quantity=quantity,
        raw=raw,
    )


def normalize_binance_order(row: dict[str, Any]) -> BrokerOrderSnapshot | None:
    raw = safe_jsonable(row)
    quantity = parse_optional_float(row.get("amount")) or parse_optional_float(
        row.get("remaining")
    )
    return BrokerOrderSnapshot(
        broker=BrokerName.BINANCE_TSM,
        order_id=str(row.get("id") or row.get("clientOrderId") or "UNKNOWN"),
        symbol=str(row.get("symbol") or "UNKNOWN"),
        side=parse_order_side(row.get("side")),
        quantity=abs(quantity or 0.0),
        status=str(row.get("status") or "open"),
        raw=raw,
    )


def normalize_binance_margins(balance: dict[str, Any]) -> tuple[BrokerMarginSnapshot, ...]:
    rows: list[BrokerMarginSnapshot] = []
    for currency in ("USDT",):
        item = balance.get(currency)
        if not isinstance(item, dict):
            continue
        rows.append(
            BrokerMarginSnapshot(
                broker=BrokerName.BINANCE_TSM,
                currency=currency,
                equity=parse_optional_float(item.get("total")),
                available=parse_optional_float(item.get("free")),
                margin_used=parse_optional_float(item.get("used")),
                raw=safe_jsonable(item),
            )
        )
    return tuple(rows)


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


def apply_side_sign(quantity: float, side: str | None) -> float:
    text = str(side or "").lower()
    if "sell" in text or "short" in text or text in {"s", "2"} or "賣" in text:
        return -abs(quantity)
    if "buy" in text or "long" in text or text in {"b", "1"} or "買" in text:
        return abs(quantity)
    return quantity


def first_text(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = row_get(row, name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def mask_account(value: Any, visible: int = 3) -> str:
    text = "" if value is None else str(value)
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * (len(text) - visible) + text[-visible:]


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
