from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import lux_trader.cli as cli_module
from lux_trader.cli import build_parser, command_fubon_exec_smoke
from lux_trader.execution import ExecutionOutcome, ExecutionOutcomeStatus
from lux_trader.execution_intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.fubon_execution import FubonFutureExecutionAdapter
from lux_trader.models import (
    BrokerName,
    Direction,
    Fill,
    OrderResult,
    OrderSide,
    OrderStatus,
    StrategyState,
)


SYMBOL = "TMFG6"


class FakeResult:
    def __init__(
        self,
        data=None,
        *,
        is_success: bool = True,
        message: str = "",
    ) -> None:
        self.data = data
        self.is_success = is_success
        self.message = message


class FakeAccount:
    account_type = "futopt"
    account = "1234567"


class FakeFutOpt:
    def __init__(
        self,
        *,
        order_results: list[dict] | None = None,
        place_result: FakeResult | None = None,
    ) -> None:
        self.order_results = order_results or []
        self.place_result = place_result
        self.place_calls: list[dict] = []
        self.get_order_results_calls = []

    def place_order(self, account, order, unblock: bool):
        self.place_calls.append(
            {
                "account": account,
                "order": order,
                "unblock": unblock,
            }
        )
        if self.place_result is not None:
            return self.place_result
        return FakeResult(
            {
                "order_no": "order-1",
                "seq_no": "seq-1",
                "symbol": order.symbol,
                "lot": order.lot,
                "status": "submitted",
            }
        )

    def get_order_results(self, account, market_type):
        self.get_order_results_calls.append((account, market_type))
        return FakeResult(self.order_results)


class FakeAccounting:
    def __init__(self, positions: list[dict] | None = None) -> None:
        self.positions = positions or []

    def query_single_position(self, account):
        return FakeResult(self.positions)


class FakeSdk:
    def __init__(
        self,
        *,
        order_results: list[dict] | None = None,
        place_result: FakeResult | None = None,
        positions: list[dict] | None = None,
    ) -> None:
        self.futopt = FakeFutOpt(
            order_results=order_results,
            place_result=place_result,
        )
        self.futopt_accounting = FakeAccounting(positions)
        self.logout_called = False

    def logout(self) -> None:
        self.logout_called = True


def ts() -> datetime:
    return datetime.fromisoformat("2026-02-02T09:15:00+08:00")


def fubon_leg(
    *,
    side: OrderSide = OrderSide.BUY,
    symbol: str = SYMBOL,
    order_type: str = ExecutionOrderType.MARKET.value,
    quantity: float = 1.0,
) -> ExecutionLeg:
    return ExecutionLeg(
        broker=BrokerName.FUBON_QFF,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=100.0,
        timestamp=ts(),
        row_index=1,
        qff_symbol=symbol,
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
        direction=Direction.SHORT_TSM_LONG_QFF,
        timestamp=ts(),
        row_index=1,
        legs=(fubon_leg(side=side, order_type=order_type),)
        if legs is None
        else legs,
        order_type=order_type,
        reason="test",
        qff_symbol=SYMBOL,
    )


def filled_row(*, filled_lot: float = 1.0, status: str = "filled") -> dict:
    return {
        "order_no": "order-1",
        "seq_no": "seq-1",
        "symbol": SYMBOL,
        "lot": 1,
        "filled_lot": filled_lot,
        "average_price": 123.0,
        "status": status,
    }


def adapter_for(fake_sdk: FakeSdk) -> FubonFutureExecutionAdapter:
    return FubonFutureExecutionAdapter(
        SYMBOL,
        sdk=fake_sdk,
        account=FakeAccount(),
        clock=ts,
        sleep=lambda _: None,
        max_poll_seconds=0,
        poll_interval_seconds=0,
    )


def test_adapter_places_entry_market_auto_order_fields() -> None:
    from fubon_neo.constant import (
        BSAction,
        FutOptMarketType,
        FutOptOrderType,
        FutOptPriceType,
        TimeInForce,
    )

    fake_sdk = FakeSdk(order_results=[filled_row()])

    outcome = adapter_for(fake_sdk).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    order = fake_sdk.futopt.place_calls[0]["order"]
    assert order.symbol == SYMBOL
    assert order.lot == 1
    assert order.buy_sell == BSAction.Buy
    assert order.market_type == FutOptMarketType.Future
    assert order.price_type == FutOptPriceType.Market
    assert order.time_in_force == TimeInForce.IOC
    assert order.order_type == FutOptOrderType.Auto
    assert order.price is None
    assert fake_sdk.futopt.place_calls[0]["unblock"] is False
    assert outcome.fills[0].price == 123.0


def test_adapter_exit_uses_close_sell_order() -> None:
    from fubon_neo.constant import BSAction, FutOptOrderType

    fake_sdk = FakeSdk(order_results=[filled_row()])

    outcome = adapter_for(fake_sdk).execute(
        execution_plan(plan_type=ExecutionPlanType.EXIT, side=OrderSide.SELL)
    )

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    order = fake_sdk.futopt.place_calls[0]["order"]
    assert order.buy_sell == BSAction.Sell
    assert order.order_type == FutOptOrderType.Close


def test_adapter_maps_partial_fill_to_paused() -> None:
    fake_sdk = FakeSdk(order_results=[filled_row(filled_lot=0.4, status="partial")])

    outcome = adapter_for(fake_sdk).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.PARTIAL_FILL
    assert outcome.recommended_state == StrategyState.PAUSED
    assert outcome.orders[0].status == OrderStatus.OPEN
    assert outcome.fills[0].quantity == 0.4


def test_adapter_maps_failed_place_order_to_failed() -> None:
    fake_sdk = FakeSdk(place_result=FakeResult(None, is_success=False, message="rejected"))

    outcome = adapter_for(fake_sdk).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FAILED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert "place_order failed" in outcome.message


def test_adapter_maps_pending_timeout_to_unknown() -> None:
    fake_sdk = FakeSdk(
        order_results=[
            {
                "order_no": "order-1",
                "symbol": SYMBOL,
                "lot": 1,
                "filled_lot": 0,
                "status": "pending",
            }
        ]
    )

    outcome = adapter_for(fake_sdk).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.UNKNOWN
    assert outcome.recommended_state == StrategyState.PAUSED
    assert outcome.orders[0].status == OrderStatus.OPEN
    assert outcome.fills == ()


@pytest.mark.parametrize(
    "plan",
    [
        execution_plan(legs=()),
        execution_plan(legs=(fubon_leg(), fubon_leg())),
        execution_plan(legs=(fubon_leg(symbol="QFFG6"),)),
        execution_plan(order_type="limit"),
        execution_plan(legs=(fubon_leg(order_type="limit"),)),
        execution_plan(legs=(fubon_leg(quantity=0.0),)),
        execution_plan(legs=(fubon_leg(quantity=1.5),)),
    ],
)
def test_adapter_rejects_invalid_plan_without_placing_order(
    plan: PairExecutionPlan,
) -> None:
    fake_sdk = FakeSdk()

    outcome = adapter_for(fake_sdk).execute(plan)

    assert outcome.status == ExecutionOutcomeStatus.REJECTED
    assert outcome.recommended_state == StrategyState.PAUSED
    assert fake_sdk.futopt.place_calls == []


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
                "binance_symbol = 'TSM/USDT:USDT'",
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
        exit_status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.FILLED,
    ) -> None:
        self.open_orders = open_orders
        self.position_quantity = position_quantity
        self.exit_status = exit_status
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
        status = (
            self.exit_status
            if plan.plan_type == ExecutionPlanType.EXIT
            else ExecutionOutcomeStatus.FILLED
        )
        fills: tuple[Fill, ...] = ()
        if status == ExecutionOutcomeStatus.FILLED:
            fills = (
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
            )
        return ExecutionOutcome(
            plan_id=plan.plan_id,
            timestamp=plan.timestamp,
            status=status,
            message="fake filled" if status == ExecutionOutcomeStatus.FILLED else "fake failed",
            orders=(
                OrderResult(
                    order_id=f"fake-{plan.plan_type.value}",
                    request=type("Request", (), {})(),
                    status=(
                        OrderStatus.FILLED
                        if status == ExecutionOutcomeStatus.FILLED
                        else OrderStatus.CANCELED
                    ),
                ),
            ),
            fills=fills,
            recommended_state=(
                None
                if status == ExecutionOutcomeStatus.FILLED
                else StrategyState.PAUSED
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
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_FUBON_EXECUTION_SMOKE", "1")


def clear_smoke_env(monkeypatch) -> None:
    monkeypatch.delenv("PROJECT_LUX_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.delenv("FUBON_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.delenv("LUX_FUBON_EXECUTION_SMOKE", raising=False)


def test_fubon_exec_smoke_requires_lot_arg(tmp_path: Path) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "fubon-exec-smoke",
                "--config",
                str(write_config(tmp_path)),
                "--symbol",
                SYMBOL,
                "--confirm-symbol",
                SYMBOL,
            ]
        )


def test_fubon_exec_smoke_rejects_missing_env_gates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clear_smoke_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-exec-smoke",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="gates closed"):
        command_fubon_exec_smoke(args)


def test_fubon_exec_smoke_rejects_symbol_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_smoke_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-exec-smoke",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--lot",
            "1",
            "--confirm-symbol",
            "TMFH6",
        ]
    )

    with pytest.raises(SystemExit, match="confirm-symbol"):
        command_fubon_exec_smoke(args)


def test_fubon_exec_smoke_rejects_existing_position(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(position_quantity=1.0)
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-exec-smoke",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="nonzero position"):
        command_fubon_exec_smoke(args)

    assert fake_adapter.executed_plans == []
    assert fake_adapter.close_called


def test_fubon_exec_smoke_rejects_existing_open_orders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(open_orders=({"id": "existing"},))
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-exec-smoke",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="existing open orders"):
        command_fubon_exec_smoke(args)

    assert fake_adapter.executed_plans == []
    assert fake_adapter.close_called


def test_fubon_exec_smoke_opens_then_closes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter()
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-exec-smoke",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    exit_code = command_fubon_exec_smoke(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Fubon execution smoke complete" in output
    assert len(fake_adapter.executed_plans) == 2
    entry, exit_plan = fake_adapter.executed_plans
    assert entry.plan_type == ExecutionPlanType.ENTRY
    assert entry.legs[0].side == OrderSide.BUY
    assert exit_plan.plan_type == ExecutionPlanType.EXIT
    assert exit_plan.legs[0].side == OrderSide.SELL
    assert exit_plan.legs[0].quantity == 1.0
    assert fake_adapter.close_called


def test_fubon_exec_smoke_close_failure_warns_manual_intervention(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(exit_status=ExecutionOutcomeStatus.FAILED)
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-exec-smoke",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    exit_code = command_fubon_exec_smoke(args)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "CRITICAL manual intervention required" in output
    assert len(fake_adapter.executed_plans) == 2
    assert fake_adapter.close_called
