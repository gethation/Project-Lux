from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ...core.models import BrokerName, OrderSide
from ...market_data.parsing import parse_optional_float
from ...reconciliation import (
    BrokerAccountSnapshot,
    BrokerMarginSnapshot,
    BrokerOrderSnapshot,
    BrokerPositionSnapshot,
)
from ..env import load_dotenv, require_env
from ..serialization import safe_jsonable


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
                if (
                    position := normalize_binance_position(
                        row,
                        self.symbol,
                    )
                )
                is not None
            ),
            open_orders=tuple(
                order
                for row in open_orders
                if (order := normalize_binance_order(row)) is not None
            ),
            margins=normalize_binance_margins(balance),
            raw={
                "exchange": "binanceusdm",
                "symbol": self.symbol,
                "position_rows": len(positions or []),
                "open_order_rows": len(open_orders or []),
            },
        )

    def fetch_margins(self) -> BrokerAccountSnapshot:
        """Balance only (equity + totalUnrealizedProfit) for the live account panel.

        Skips fetch_positions / fetch_open_orders so the per-minute panel refresh
        stays a single lightweight private call.
        """
        exchange = self._ensure_exchange()
        balance = exchange.fetch_balance()
        return BrokerAccountSnapshot(
            broker=self.broker,
            account_id="BINANCE_USDM",
            fetched_at=self.clock(),
            margins=normalize_binance_margins(balance),
            raw={"exchange": "binanceusdm", "symbol": self.symbol},
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
        self.exchange.load_markets()
        return self.exchange


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


def normalize_binance_order(
    row: dict[str, Any],
) -> BrokerOrderSnapshot | None:
    raw = safe_jsonable(row)
    quantity = parse_optional_float(row.get("amount"))
    if quantity is None:
        quantity = parse_optional_float(row.get("remaining"))
    return BrokerOrderSnapshot(
        broker=BrokerName.BINANCE_TSM,
        order_id=str(row.get("id") or row.get("clientOrderId") or "UNKNOWN"),
        symbol=str(row.get("symbol") or "UNKNOWN"),
        side=parse_order_side(row.get("side")),
        quantity=abs(quantity or 0.0),
        status=str(row.get("status") or "open"),
        raw=raw,
    )


def normalize_binance_margins(
    balance: dict[str, Any],
) -> tuple[BrokerMarginSnapshot, ...]:
    # /fapi account-level figures live in ccxt's balance["info"]; keep the
    # margin fields margin management needs (equity incl. unrealized PnL and
    # maintenance margin) in raw so BrokerMarginSnapshot stays unchanged.
    # totalUnrealizedProfit lets the live account panel read position uPnL
    # directly (falls back to totalMarginBalance - totalWalletBalance).
    info = balance.get("info")
    account_fields: dict[str, Any] = {}
    if isinstance(info, dict):
        for name in (
            "totalMarginBalance",
            "totalMaintMargin",
            "totalWalletBalance",
            "availableBalance",
            "totalUnrealizedProfit",
        ):
            if info.get(name) is not None:
                account_fields[name] = info.get(name)
    rows: list[BrokerMarginSnapshot] = []
    for currency in ("USDT",):
        item = balance.get(currency)
        if not isinstance(item, dict):
            continue
        raw = dict(safe_jsonable(item) or {})
        raw.update(safe_jsonable(account_fields) or {})
        total_margin_balance = parse_optional_float(
            account_fields.get("totalMarginBalance")
        )
        rows.append(
            BrokerMarginSnapshot(
                broker=BrokerName.BINANCE_TSM,
                currency=currency,
                equity=(
                    total_margin_balance
                    if total_margin_balance is not None
                    else parse_optional_float(item.get("total"))
                ),
                available=parse_optional_float(item.get("free")),
                margin_used=parse_optional_float(item.get("used")),
                raw=raw,
            )
        )
    return tuple(rows)


def parse_order_side(value: Any) -> OrderSide | None:
    text = str(value or "").lower()
    if "buy" in text or text in {"b", "1"}:
        return OrderSide.BUY
    if "sell" in text or text in {"s", "2"}:
        return OrderSide.SELL
    return None


__all__ = [
    "BinanceReadOnlyBroker",
    "normalize_binance_margins",
    "normalize_binance_order",
    "normalize_binance_position",
]
