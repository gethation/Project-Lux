from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import lux_trader.cli.commands_execution as cli_module
from lux_trader.integrations.binance.execution import (
    BinanceTsmExecutionAdapter,
    normalize_binance_order_quantity,
)
from lux_trader.cli.parser import build_parser
from lux_trader.cli.commands_execution import command_exec_smoke as command_binance_exec_smoke
from lux_trader.execution import ExecutionOutcome, ExecutionOutcomeStatus
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.core.models import (
    BrokerName,
    Direction,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)


SYMBOL = "TSM/USDT:USDT"


class FakeExchange:
    def __init__(
        self,
        *,
        create_order_response: dict | None = None,
        fetch_order_response: dict | None = None,
        fetch_order_responses: list[dict] | None = None,
        create_error: Exception | None = None,
        fetch_error: Exception | None = None,
        client_lookup_response: dict | None = None,
        client_lookup_error: Exception | None = None,
        open_orders: list[dict] | None = None,
        positions: list[dict] | None = None,
        position_results: list[list[dict]] | None = None,
        current_margin_mode: str | None = None,
        current_leverage: int | None = None,
        precision_quantity: str | None = None,
        minimum_amount: float | None = None,
    ) -> None:
        self.create_order_response = create_order_response
        self.fetch_order_response = fetch_order_response
        self.fetch_order_responses = fetch_order_responses
        self.create_error = create_error
        self.fetch_error = fetch_error
        self.client_lookup_response = client_lookup_response
        self.client_lookup_error = client_lookup_error
        self.open_orders = open_orders or []
        self.positions = positions or []
        self.position_results = position_results
        self.current_margin_mode = current_margin_mode
        self.current_leverage = current_leverage
        self.precision_quantity = precision_quantity
        self.minimum_amount = minimum_amount
        self.create_calls: list[dict] = []
        self.fetch_calls: list[dict] = []
        self.client_lookup_calls: list[dict] = []
        self.position_calls = 0
        self.set_margin_mode_calls: list[tuple[str, str]] = []
        self.set_leverage_calls: list[tuple[int, str]] = []
        self.load_markets_called = False
        self.close_called = False

    def load_markets(self) -> None:
        self.load_markets_called = True

    def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price,
        params: dict,
    ) -> dict:
        if self.create_error is not None:
            raise self.create_error
        self.create_calls.append(
            {
                "symbol": symbol,
                "order_type": order_type,
                "side": side,
                "amount": amount,
                "price": price,
                "params": params,
            }
        )
        return self.create_order_response or {
            "id": "order-1",
            "status": "open",
            "amount": amount,
        }

    def fetch_order(self, order_id, symbol: str, params: dict | None = None) -> dict:
        if params and params.get("origClientOrderId"):
            self.client_lookup_calls.append(dict(params))
            if self.client_lookup_error is not None:
                raise self.client_lookup_error
            if self.client_lookup_response is None:
                raise TypeError("client order id lookup not configured")
            return self.client_lookup_response
        self.fetch_calls.append({"order_id": order_id, "symbol": symbol})
        if self.fetch_error is not None:
            raise self.fetch_error
        if self.fetch_order_responses is not None:
            index = min(len(self.fetch_calls) - 1, len(self.fetch_order_responses) - 1)
            return self.fetch_order_responses[index]
        amount = self.create_calls[-1]["amount"] if self.create_calls else 1.0
        return self.fetch_order_response or {
            "id": order_id,
            "symbol": symbol,
            "status": "closed",
            "amount": amount,
            "filled": amount,
            "average": 123.45,
            "fee": {"cost": 0.01, "currency": "USDT"},
        }

    def fetch_open_orders(self, symbol: str) -> list[dict]:
        return self.open_orders

    def fetch_positions(self, symbols: list[str]) -> list[dict]:
        self.position_calls += 1
        if self.position_results is not None:
            index = min(self.position_calls - 1, len(self.position_results) - 1)
            return self.position_results[index]
        return self.positions

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return self.precision_quantity or str(amount)

    def market(self, symbol: str) -> dict:
        return {
            "symbol": symbol,
            "limits": {"amount": {"min": self.minimum_amount}},
        }

    def fetch_margin_mode(self, symbol: str) -> dict:
        return {"symbol": symbol, "marginMode": self.current_margin_mode}

    def fetch_leverage(self, symbol: str) -> dict:
        return {"symbol": symbol, "leverage": self.current_leverage}

    def set_margin_mode(self, margin_mode: str, symbol: str) -> None:
        self.set_margin_mode_calls.append((margin_mode, symbol))
        self.current_margin_mode = margin_mode

    def set_leverage(self, leverage: int, symbol: str) -> None:
        self.set_leverage_calls.append((leverage, symbol))
        self.current_leverage = leverage

    def close(self) -> None:
        self.close_called = True


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


def binance_leg(
    *,
    side: OrderSide = OrderSide.BUY,
    symbol: str = SYMBOL,
    order_type: str = ExecutionOrderType.MARKET.value,
    quantity: float = 0.01,
) -> ExecutionLeg:
    return ExecutionLeg(
        broker=BrokerName.BINANCE_TSM,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=100.0,
        timestamp=ts(),
        row_index=1,
        order_type=order_type,
    )


def execution_plan(
    *,
    plan_type: ExecutionPlanType = ExecutionPlanType.ENTRY,
    side: OrderSide = OrderSide.BUY,
    legs: tuple[ExecutionLeg, ...] | None = None,
    order_type: str = ExecutionOrderType.MARKET.value,
) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id=f"PLAN-{plan_type.value}",
        plan_type=plan_type,
        direction=Direction.LONG_TSM_SHORT_QFF,
        timestamp=ts(),
        row_index=1,
        legs=(
            (binance_leg(side=side, order_type=order_type),)
            if legs is None
            else legs
        ),
        order_type=order_type,
        reason="test",
    )


def test_adapter_loads_env_and_places_entry_market_without_reduce_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "BINANCE_API_KEY=test-key\nBINANCE_SECRET=test-secret\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_SECRET", raising=False)
    captured_options = {}
    fake_exchange = FakeExchange()

    def factory(options: dict):
        captured_options.update(options)
        return fake_exchange

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        env_path,
        exchange_factory=factory,
        clock=ts,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.filled
    assert fake_exchange.load_markets_called
    assert captured_options["apiKey"] == "test-key"
    assert captured_options["secret"] == "test-secret"
    assert len(fake_exchange.create_calls) == 1
    create_call = fake_exchange.create_calls[0]
    assert create_call["symbol"] == SYMBOL
    assert create_call["order_type"] == "market"
    assert create_call["side"] == "buy"
    assert create_call["amount"] == 0.01
    assert create_call["price"] is None
    assert "reduceOnly" not in create_call["params"]
    # every order carries a pre-assigned client id for create-timeout recovery
    assert create_call["params"]["newClientOrderId"].startswith("LUX-")
    assert outcome.fills[0].price == 123.45


def test_adapter_exit_order_uses_reduce_only() -> None:
    fake_exchange = FakeExchange()
    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
    ).execute(execution_plan(plan_type=ExecutionPlanType.EXIT, side=OrderSide.SELL))

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    params = fake_exchange.create_calls[0]["params"]
    assert params["reduceOnly"] is True
    assert params["newClientOrderId"].startswith("LUX-")


def test_adapter_normalizes_quantity_before_order_and_records_actual_request() -> None:
    fake_exchange = FakeExchange(precision_quantity="0.012", minimum_amount=0.001)

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
    ).execute(
        execution_plan(legs=(binance_leg(quantity=0.01234),))
    )

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert fake_exchange.create_calls[0]["amount"] == 0.012
    assert outcome.orders[0].request.quantity == 0.012
    assert outcome.fills[0].quantity == 0.012
    assert outcome.payload is not None
    assert outcome.payload["original_requested_quantity"] == 0.01234
    assert outcome.payload["requested_quantity"] == 0.012


@pytest.mark.parametrize(
    ("precision_quantity", "minimum_amount", "message"),
    [
        ("0", None, "becomes zero"),
        ("0.001", 0.01, "below minimum"),
    ],
)
def test_adapter_rejects_invalid_normalized_quantity_without_order(
    precision_quantity: str,
    minimum_amount: float | None,
    message: str,
) -> None:
    fake_exchange = FakeExchange(
        precision_quantity=precision_quantity,
        minimum_amount=minimum_amount,
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert message in outcome.message
    assert fake_exchange.create_calls == []


def test_normalize_binance_quantity_uses_market_metadata_fallback() -> None:
    exchange = type(
        "Exchange",
        (),
        {
            "markets": {
                SYMBOL: {"limits": {"amount": {"min": 0.01}}},
            },
            "amount_to_precision": lambda self, symbol, amount: "0.02",
        },
    )()

    assert normalize_binance_order_quantity(exchange, SYMBOL, 0.023) == 0.02


def test_adapter_skips_margin_and_leverage_set_when_current_values_match() -> None:
    fake_exchange = FakeExchange(current_margin_mode="cross", current_leverage=1)

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        leverage=1,
        margin_mode="cross",
        enforce_leverage=True,
        clock=ts,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert fake_exchange.set_margin_mode_calls == []
    assert fake_exchange.set_leverage_calls == []
    assert len(fake_exchange.create_calls) == 1


def test_adapter_sets_margin_and_leverage_when_current_values_differ() -> None:
    fake_exchange = FakeExchange(current_margin_mode="isolated", current_leverage=3)

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        leverage=1,
        margin_mode="cross",
        enforce_leverage=True,
        clock=ts,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert fake_exchange.set_margin_mode_calls == [("cross", SYMBOL)]
    assert fake_exchange.set_leverage_calls == [(1, SYMBOL)]
    assert len(fake_exchange.create_calls) == 1


def test_adapter_maps_partial_fill_to_paused() -> None:
    fake_exchange = FakeExchange(
        fetch_order_response={
            "id": "order-1",
            "status": "open",
            "amount": 0.01,
            "filled": 0.004,
            "average": 124.0,
        }
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
        max_poll_seconds=0,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.PARTIAL_FILL
    assert outcome.recommended_state == StrategyState.PAUSED
    assert outcome.orders[0].status == OrderStatus.OPEN
    assert outcome.fills[0].quantity == 0.004


def test_adapter_maps_canceled_order_to_failed() -> None:
    fake_exchange = FakeExchange(
        fetch_order_response={
            "id": "order-1",
            "status": "canceled",
            "amount": 0.01,
            "filled": 0.0,
        }
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert outcome.orders[0].status == OrderStatus.CANCELED
    assert outcome.fills == ()


def test_adapter_create_order_exception_is_failed_when_lookup_confirms_absence() -> None:
    class OrderNotFound(Exception):
        pass

    fake_exchange = FakeExchange(
        create_error=RuntimeError("exchange rejected"),
        client_lookup_error=OrderNotFound("Order does not exist"),
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "create_order failed" in outcome.message
    assert outcome.payload["recovery"]["status"] == "not_found"
    assert outcome.payload["client_order_id"].startswith("LUX-")
    # the client order id was pre-assigned to the create request... which
    # failed, so it only appears in the recovery lookup
    assert fake_exchange.client_lookup_calls


def test_adapter_create_exception_recovers_filled_order_by_client_order_id() -> None:
    fake_exchange = FakeExchange(
        create_error=RuntimeError("read timeout"),
        client_lookup_response={
            "id": "order-77",
            "status": "closed",
            "amount": 0.01,
            "filled": 0.01,
            "average": 125.0,
        },
        fetch_order_response={
            "id": "order-77",
            "status": "closed",
            "amount": 0.01,
            "filled": 0.01,
            "average": 125.0,
        },
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.recommended_state is None
    assert outcome.fills[0].quantity == 0.01
    assert outcome.payload["recovery"]["status"] == "found"


def test_adapter_create_exception_with_failed_lookup_uses_position_delta() -> None:
    fake_exchange = FakeExchange(
        create_error=RuntimeError("read timeout"),
        client_lookup_error=RuntimeError("lookup also down"),
        position_results=[
            [],  # before order: flat
            [  # after order: the position exists — the order actually filled
                {
                    "symbol": SYMBOL,
                    "info": {"symbol": "TSMUSDT", "positionAmt": "0.01"},
                }
            ],
        ],
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
        recovery_attempts=1,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.payload["fill_source"] == "position_delta"
    assert outcome.payload["recovery"]["status"] == "error"
    assert outcome.fills[0].quantity == pytest.approx(0.01)


def test_adapter_create_exception_without_any_evidence_is_unknown() -> None:
    fake_exchange = FakeExchange(
        create_error=RuntimeError("read timeout"),
        client_lookup_error=RuntimeError("lookup also down"),
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
        recovery_attempts=1,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "create_order outcome unknown" in outcome.message
    assert outcome.payload["recovery"]["status"] == "error"


def test_adapter_fetch_order_exception_is_unknown() -> None:
    fake_exchange = FakeExchange(fetch_error=RuntimeError("network timeout"))

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
        max_poll_seconds=1.0,
        poll_interval_seconds=0.5,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
    assert outcome.recommended_state == StrategyState.PAUSED
    assert outcome.orders[0].status == OrderStatus.OPEN
    # every fetch attempt is recorded for audit
    assert len(outcome.payload["poll_errors"]) == 3


def test_adapter_polling_confirms_fill_on_second_fetch_attempt() -> None:
    fake_exchange = FakeExchange(
        fetch_order_responses=[
            {"id": "order-1", "status": "open", "amount": 0.01, "filled": 0.0},
            {
                "id": "order-1",
                "status": "closed",
                "amount": 0.01,
                "filled": 0.01,
                "average": 124.5,
            },
        ]
    )

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
        sleep=lambda _: None,
        max_poll_seconds=1.0,
        poll_interval_seconds=0.5,
    ).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.payload["fill_source"] == "order_result"
    assert len(fake_exchange.fetch_calls) == 2


@pytest.mark.parametrize(
    "plan",
    [
        execution_plan(legs=()),
        execution_plan(legs=(binance_leg(), binance_leg())),
        execution_plan(legs=(binance_leg(symbol="OTHER/USDT:USDT"),)),
        execution_plan(order_type="limit"),
        execution_plan(legs=(binance_leg(order_type="limit"),)),
        execution_plan(legs=(binance_leg(quantity=0.0),)),
    ],
)
def test_adapter_rejects_invalid_plan_without_calling_exchange(
    plan: PairExecutionPlan,
) -> None:
    fake_exchange = FakeExchange()

    outcome = BinanceTsmExecutionAdapter(
        SYMBOL,
        exchange=fake_exchange,
        clock=ts,
    ).execute(plan)

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert fake_exchange.create_calls == []


def write_config(
    tmp_path: Path,
    *,
    allow_live_order: bool = True,
    live_execution_enabled: bool = True,
) -> Path:
    config_path = tmp_path / "config.test.toml"
    config_path.write_text(
        "\n".join(
            [
                "[paths]",
                "input_csv = ''",
                f"store_path = '{(tmp_path / 'store.sqlite3').as_posix()}'",
                "",
                "[safety]",
                f"allow_live_order = {str(allow_live_order).lower()}",
                "",
                "[live_market_data]",
                "qff_symbol = 'QFFG6'",
                f"binance_symbol = '{SYMBOL}'",
                "fubon_env_path = '.env'",
                f"taifex_cache_dir = '{(tmp_path / 'taifex').as_posix()}'",
                "",
                "[live_execution]",
                f"enabled = {str(live_execution_enabled).lower()}",
                "qff_first = true",
            ]
        ),
        encoding="utf-8",
    )
    return config_path


class FakeSmokeAdapter:
    def __init__(
        self,
        *,
        open_orders: tuple[dict, ...] = (),
        position_quantity: float = 0.0,
    ) -> None:
        self.open_orders = open_orders
        self.position_quantity = position_quantity
        self.executed_plans: list[PairExecutionPlan] = []
        self.close_called = False

    def preflight(self):
        return type(
            "Preflight",
            (),
            {
                "open_orders": self.open_orders,
                "position_quantity": self.position_quantity,
            },
        )()

    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome:
        self.executed_plans.append(plan)
        leg = plan.legs[0]
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=ExecutionOutcomeStatus.FILLED,
            message="fake filled",
            orders=(
                OrderResult(
                    order_id=f"fake-{plan.plan_type.value}",
                    request=type("Request", (), {})(),
                    status=OrderStatus.FILLED,
                ),
            ),
            fills=(
                Fill(
                    fill_id=f"fill-{plan.plan_type.value}",
                    order_id=f"fake-{plan.plan_type.value}",
                    broker=leg.broker,
                    symbol=leg.symbol,
                    side=leg.side,
                    quantity=leg.quantity,
                    price=100.0,
                    fee_twd=0.0,
                    timestamp=plan.timestamp,
                    row_index=plan.row_index,
                ),
            ),
        )

    def fetch_open_orders(self):
        return ()

    def fetch_position_quantity(self) -> float:
        return 0.0

    def close(self) -> None:
        self.close_called = True


def set_smoke_env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("BINANCE_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_BINANCE_EXECUTION_SMOKE", "1")


def test_binance_exec_smoke_requires_quantity_arg(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "exec-smoke", "--venue", "binance",
            "--config",
            str(write_config(tmp_path)),
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="--quantity is required"):
        command_binance_exec_smoke(args)


def test_binance_exec_smoke_rejects_missing_env_gates(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "exec-smoke", "--venue", "binance",
            "--config",
            str(write_config(tmp_path)),
            "--quantity",
            "0.01",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="gates closed"):
        command_binance_exec_smoke(args)


def test_binance_exec_smoke_rejects_symbol_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_smoke_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "exec-smoke", "--venue", "binance",
            "--config",
            str(write_config(tmp_path)),
            "--quantity",
            "0.01",
            "--confirm-symbol",
            "OTHER/USDT:USDT",
        ]
    )

    with pytest.raises(SystemExit, match="confirm-symbol"):
        command_binance_exec_smoke(args)


def test_binance_exec_smoke_rejects_existing_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(position_quantity=1.0)
    monkeypatch.setattr(
        cli_module,
        "BinanceTsmExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "exec-smoke", "--venue", "binance",
            "--config",
            str(write_config(tmp_path)),
            "--quantity",
            "0.01",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="nonzero position"):
        command_binance_exec_smoke(args)

    assert fake_adapter.executed_plans == []
    assert fake_adapter.close_called


def test_binance_exec_smoke_rejects_existing_open_orders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(open_orders=({"id": "existing"},))
    monkeypatch.setattr(
        cli_module,
        "BinanceTsmExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "exec-smoke", "--venue", "binance",
            "--config",
            str(write_config(tmp_path)),
            "--quantity",
            "0.01",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="existing open orders"):
        command_binance_exec_smoke(args)

    assert fake_adapter.executed_plans == []
    assert fake_adapter.close_called


def test_binance_exec_smoke_opens_then_reduce_only_closes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter()
    monkeypatch.setattr(
        cli_module,
        "BinanceTsmExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "exec-smoke", "--venue", "binance",
            "--config",
            str(write_config(tmp_path)),
            "--quantity",
            "0.01",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    exit_code = command_binance_exec_smoke(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Binance execution smoke complete" in output
    assert len(fake_adapter.executed_plans) == 2
    entry, exit_plan = fake_adapter.executed_plans
    assert entry.plan_type == ExecutionPlanType.ENTRY
    assert entry.legs[0].side == OrderSide.BUY
    assert exit_plan.plan_type == ExecutionPlanType.EXIT
    assert exit_plan.legs[0].side == OrderSide.SELL
    assert exit_plan.legs[0].quantity == 0.01
    assert fake_adapter.close_called
