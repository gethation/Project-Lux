from __future__ import annotations

from datetime import datetime

import pytest

from lux_trader.core.models import BrokerName, OrderSide
from lux_trader.integrations.binance.readonly import (
    BinanceReadOnlyBroker,
    normalize_binance_position,
)
from lux_trader.integrations.fubon.auth import (
    checked_result_data,
    select_futopt_account,
)
from lux_trader.integrations.fubon.readonly import (
    FubonReadOnlyBroker,
    normalize_fubon_margin,
    normalize_fubon_order,
    normalize_fubon_position,
)


class FakeResult:
    def __init__(self, data, *, is_success: bool = True, message: str = "") -> None:
        self.data = data
        self.is_success = is_success
        self.message = message


class FakeAccount:
    account_type = "futopt"
    branch_no = "15000"
    account = "123456789"


class FakeFutoptAccounting:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def query_margin_equity(self, account):
        self.calls.append("query_margin_equity")
        return FakeResult(
            [
                {
                    "currency": "TWD",
                    "equity": 1_500_000,
                    "available_margin": 1_200_000,
                    "initial_margin": 300_000,
                }
            ]
        )

    def query_single_position(self, account):
        self.calls.append("query_single_position")
        return FakeResult(
            [
                {
                    "symbol": "QFFG6",
                    "buy_lot": 3,
                    "sell_lot": 1,
                }
            ]
        )


class FakeFutopt:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def get_order_results(self, account, market_type):
        self.calls.append(market_type)
        return FakeResult(
            [
                {
                    "order_id": f"ORD-{len(self.calls)}",
                    "symbol": "QFFG6",
                    "buy_sell": "Buy",
                    "lot": 1,
                    "status": "open",
                }
            ]
        )


class FakeFubonSdk:
    def __init__(self) -> None:
        self.futopt_accounting = FakeFutoptAccounting()
        self.futopt = FakeFutopt()
        self.logged_out = False

    def logout(self) -> None:
        self.logged_out = True


class FakeBinanceExchange:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    def load_markets(self) -> None:
        self.calls.append("load_markets")

    def fetch_balance(self):
        self.calls.append("fetch_balance")
        return {"USDT": {"total": 1000.0, "free": 900.0, "used": 100.0}}

    def fetch_positions(self, symbols):
        self.calls.append(f"fetch_positions:{symbols[0]}")
        return [
            {
                "symbol": "TSM/USDT:USDT",
                "contracts": 0.0,
                "side": "short",
                "info": {"positionAmt": "-12.5"},
            }
        ]

    def fetch_open_orders(self, symbol):
        self.calls.append(f"fetch_open_orders:{symbol}")
        return [
            {
                "id": "BINANCE-1",
                "symbol": symbol,
                "side": "sell",
                "amount": 2.5,
                "status": "open",
            }
        ]

    def close(self) -> None:
        self.closed = True


def test_fubon_readonly_broker_fetches_margin_positions_and_orders() -> None:
    sdk = FakeFubonSdk()
    broker = FubonReadOnlyBroker(
        sdk=sdk,
        accounts=[FakeAccount()],
        clock=lambda: datetime.fromisoformat("2026-06-18T09:00:00+08:00"),
    )

    snapshot = broker.fetch_snapshot()
    broker.close()

    assert snapshot.broker == BrokerName.FUBON_QFF
    assert snapshot.account_id == "******789"
    assert snapshot.positions[0].symbol == "QFFG6"
    assert snapshot.positions[0].quantity == 2
    assert len(snapshot.open_orders) == 2
    assert snapshot.open_orders[0].side == OrderSide.BUY
    assert snapshot.margins[0].equity == 1_500_000
    assert sdk.futopt_accounting.calls == [
        "query_margin_equity",
        "query_single_position",
    ]
    assert len(sdk.futopt.calls) == 2
    assert sdk.logged_out


def test_fubon_requires_futopt_account() -> None:
    class StockAccount:
        account_type = "stock"

    with pytest.raises(RuntimeError, match="no futopt account"):
        select_futopt_account([StockAccount()])


def test_fubon_order_normalizer_skips_final_orders() -> None:
    assert normalize_fubon_order({"status": "filled", "symbol": "QFFG6"}) is None
    assert normalize_fubon_order({"status": "canceled", "symbol": "QFFG6"}) is None
    assert normalize_fubon_order({"status": "open", "symbol": "QFFG6"}) is not None


def test_fubon_position_normalizer_parses_official_lot_fields() -> None:
    position = normalize_fubon_position(
        {
            "symbol": "FIQFF",
            "expiry_date": 202607,
            "buy_sell": "Buy",
            "orig_lots": 1,
            "tradable_lot": 1,
        },
        expected_symbol="QFFG6",
    )

    assert position is not None
    assert position.symbol == "QFFG6"
    assert position.quantity == 1
    assert position.raw["symbol"] == "FIQFF"


def test_fubon_position_normalizer_applies_sell_side_to_official_lot_fields() -> None:
    position = normalize_fubon_position(
        {
            "symbol": "FIQFF",
            "expiry_date": 202607,
            "buy_sell": "Sell",
            "orig_lots": 2,
        },
        expected_symbol="QFFG6",
    )

    assert position is not None
    assert position.symbol == "QFFG6"
    assert position.quantity == -2


def test_fubon_margin_normalizer_parses_value_json_string() -> None:
    margin = normalize_fubon_margin(
        {
            "value": (
                '{"currency":"TWD","today_equity":60110.0,'
                '"available_margin":60110.0,"initial_margin":0.0}'
            )
        }
    )

    assert margin is not None
    assert margin.currency == "TWD"
    assert margin.equity == 60110.0
    assert margin.available == 60110.0
    assert margin.margin_used == 0.0


def test_fubon_empty_position_result_can_be_treated_as_empty() -> None:
    result = FakeResult(None, is_success=False, message="查無任何資料")

    assert checked_result_data(result, "single_position", empty_ok=True) == []
    with pytest.raises(RuntimeError, match="single_position failed"):
        checked_result_data(result, "single_position")


def test_binance_readonly_broker_fetches_balance_positions_and_orders() -> None:
    exchange = FakeBinanceExchange()
    broker = BinanceReadOnlyBroker(
        "TSM/USDT:USDT",
        exchange=exchange,
        clock=lambda: datetime.fromisoformat("2026-06-18T09:00:00+08:00"),
    )

    snapshot = broker.fetch_snapshot()
    broker.close()

    assert snapshot.broker == BrokerName.BINANCE_TSM
    assert snapshot.positions[0].symbol == "TSM/USDT:USDT"
    assert snapshot.positions[0].quantity == -12.5
    assert snapshot.open_orders[0].order_id == "BINANCE-1"
    assert snapshot.open_orders[0].side == OrderSide.SELL
    assert snapshot.margins[0].currency == "USDT"
    assert snapshot.margins[0].available == 900.0
    assert exchange.calls == [
        "fetch_balance",
        "fetch_positions:TSM/USDT:USDT",
        "fetch_open_orders:TSM/USDT:USDT",
    ]
    assert exchange.closed


def test_binance_exchange_factory_uses_env_credentials(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def exchange_factory(options):
        captured.update(options)
        return FakeBinanceExchange()

    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SECRET", "secret")
    broker = BinanceReadOnlyBroker(
        "TSM/USDT:USDT",
        exchange_factory=exchange_factory,
    )

    broker.fetch_snapshot()

    assert captured["apiKey"] == "key"
    assert captured["secret"] == "secret"
    assert captured["enableRateLimit"] is True


def test_binance_missing_env_fails_fast(monkeypatch) -> None:
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    broker = BinanceReadOnlyBroker("TSM/USDT:USDT")

    with pytest.raises(RuntimeError, match="BINANCE_API_KEY"):
        broker.fetch_snapshot()


def test_binance_position_normalizer_ignores_other_symbols() -> None:
    assert (
        normalize_binance_position(
            {"symbol": "BTC/USDT:USDT", "info": {"positionAmt": "1"}},
            "TSM/USDT:USDT",
        )
        is None
    )
