from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

import lux_trader.cli as cli_module
from lux_trader.cli import (
    build_parser,
    command_fubon_exec_smoke,
    command_fubon_manual_close,
    command_fubon_order_records,
)
from lux_trader.execution import ExecutionOutcome, ExecutionOutcomeStatus
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionOrderType,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.integrations.fubon.contracts import (
    FubonContractIdentity,
    contract_month_from_symbol,
    fubon_symbol_matches,
)
from lux_trader.integrations.fubon.execution import (
    FubonFutureExecutionAdapter,
    map_fubon_order_status,
)
from lux_trader.integrations.fubon.parsing import fubon_raw_row
from lux_trader.core.models import (
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


def fubon_repr_order_result() -> dict:
    return {
        "value": """FutOptOrderResult {
    function_type: None,
    date: "2026/06/25",
    seq_no: "00110342383",
    branch_no: "15000",
    account: "8246253",
    order_no: "s0E1X",
    asset_type: 1,
    market: "TAIMEX",
    market_type: Future,
    symbol: "FITM",
    expiry_date: "202607",
    buy_sell: Buy,
    price_type: Market,
    price: 0,
    lot: 1,
    after_lot: 1,
    time_in_force: IOC,
    order_type: New,
    status: 50,
    is_pre_order: false,
    filled_lot: 1,
    filled_money: 46497,
    user_def: "Projec",
    last_time: "13:35:47.000",
    details: None,
    error_message: None,
}"""
    }


def qff_repr_order_result() -> dict:
    return {
        "value": """FutOptOrderResult {
    order_no: "qff-order",
    seq_no: "qff-seq",
    symbol: "QFF",
    expiry_date: "202607",
    buy_sell: Sell,
    lot: 2,
    status: 50,
    filled_lot: 2,
    filled_money: 480000,
}"""
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


def test_adapter_matches_fubon_repr_order_result_symbol_and_expiry() -> None:
    fake_sdk = FakeSdk(order_results=[fubon_repr_order_result()])

    outcome = adapter_for(fake_sdk).execute(execution_plan())

    assert outcome.status == ExecutionOutcomeStatus.FILLED
    assert outcome.fills[0].quantity == 1
    assert outcome.fills[0].price == 46497.0
    assert outcome.payload["final_order"]["symbol"] == "FITM"
    assert outcome.payload["final_order"]["expiry_date"] == "202607"


def test_adapter_fetch_order_records_filters_by_contract_identity() -> None:
    other_tmf_month = {
        "value": """FutOptOrderResult {
    order_no: "other-month",
    symbol: "FITM",
    expiry_date: "202608",
    buy_sell: Buy,
    lot: 1,
    status: 50,
    filled_lot: 1,
}"""
    }
    fake_sdk = FakeSdk(
        order_results=[
            other_tmf_month,
            qff_repr_order_result(),
            fubon_repr_order_result(),
        ]
    )

    records = adapter_for(fake_sdk).fetch_order_records()

    assert len(records) == 1
    assert records[0]["order_no"] == "s0E1X"
    assert records[0]["symbol"] == "FITM"


def test_fubon_repr_row_parser_and_tmf_symbol_match() -> None:
    raw = fubon_raw_row(fubon_repr_order_result())

    assert raw["symbol"] == "FITM"
    assert raw["filled_lot"] == 1
    assert raw["is_pre_order"] is False
    assert contract_month_from_symbol("TMFG6", reference_date=date(2026, 6, 25)) == "202607"
    assert fubon_symbol_matches(raw, "TMFG6")


def test_contract_identity_supports_tmf_and_qff_aliases() -> None:
    tmf = FubonContractIdentity.from_symbol(
        "TMFG6",
        reference_date=date(2026, 6, 25),
    )
    qff = FubonContractIdentity.from_symbol(
        "QFFG6",
        reference_date=date(2026, 6, 25),
    )

    assert tmf.product == "TMF"
    assert tmf.contract_month == "202607"
    assert "FITM" in tmf.broker_symbols
    assert tmf.matches(fubon_raw_row(fubon_repr_order_result()), side=OrderSide.BUY, lot=1)
    assert qff.product == "QFF"
    assert qff.contract_month == "202607"
    assert qff.matches(fubon_raw_row(qff_repr_order_result()), side=OrderSide.SELL, lot=2)


def test_fubon_status_mapping_uses_fill_quantity_before_status_code() -> None:
    assert (
        map_fubon_order_status(status_text="50", requested=1.0, filled=1.0)
        == ExecutionOutcomeStatus.FILLED
    )
    assert (
        map_fubon_order_status(status_text="50", requested=2.0, filled=1.0)
        == ExecutionOutcomeStatus.PARTIAL_FILL
    )
    assert (
        map_fubon_order_status(status_text="90", requested=1.0, filled=0.0)
        == ExecutionOutcomeStatus.FAILED
    )
    assert (
        map_fubon_order_status(status_text="10", requested=1.0, filled=0.0)
        == ExecutionOutcomeStatus.UNKNOWN
    )


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
        position_quantities: tuple[float, ...] = (1.0, 0.0),
        order_records: tuple[dict, ...] = (
            {
                "order_no": "fake-entry",
                "symbol": SYMBOL,
                "buy_sell": "Buy",
                "status": "filled",
                "lot": 1,
                "filled_lot": 1,
                "average_price": 100.0,
            },
            {
                "order_no": "fake-exit",
                "symbol": SYMBOL,
                "buy_sell": "Sell",
                "status": "filled",
                "lot": 1,
                "filled_lot": 1,
                "average_price": 100.0,
            },
        ),
        entry_status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.FILLED,
        exit_status: ExecutionOutcomeStatus = ExecutionOutcomeStatus.FILLED,
    ) -> None:
        self.open_orders = open_orders
        self.position_quantity = position_quantity
        self.position_quantities = position_quantities
        self.position_query_count = 0
        self.order_records = order_records
        self.entry_status = entry_status
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
            else self.entry_status
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
        return self.open_orders

    def fetch_position_quantity(self) -> float:
        self.position_query_count += 1
        if self.position_quantities:
            index = min(self.position_query_count - 1, len(self.position_quantities) - 1)
            return self.position_quantities[index]
        return self.position_quantity

    def fetch_order_records(self):
        return self.order_records

    def close(self) -> None:
        self.close_called = True


def set_smoke_env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_FUBON_EXECUTION_SMOKE", "1")


def set_manual_close_env(monkeypatch) -> None:
    monkeypatch.setenv("PROJECT_LUX_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("FUBON_ALLOW_LIVE_ORDER", "1")
    monkeypatch.setenv("LUX_FUBON_MANUAL_CLOSE", "1")


def clear_smoke_env(monkeypatch) -> None:
    monkeypatch.delenv("PROJECT_LUX_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.delenv("FUBON_ALLOW_LIVE_ORDER", raising=False)
    monkeypatch.delenv("LUX_FUBON_EXECUTION_SMOKE", raising=False)
    monkeypatch.delenv("LUX_FUBON_MANUAL_CLOSE", raising=False)


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
            "--raw-json",
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
    assert "Fubon execution smoke after_entry: position=1, open_orders=0" in output
    assert "Fubon execution smoke after_exit: position=0, open_orders=0" in output
    assert "Fubon execution smoke order_records: count=2" in output
    assert "fake-entry" in output
    assert len(fake_adapter.executed_plans) == 2
    entry, exit_plan = fake_adapter.executed_plans
    assert entry.plan_type == ExecutionPlanType.ENTRY
    assert entry.legs[0].side == OrderSide.BUY
    assert exit_plan.plan_type == ExecutionPlanType.EXIT
    assert exit_plan.legs[0].side == OrderSide.SELL
    assert exit_plan.legs[0].quantity == 1.0
    assert fake_adapter.close_called


def test_fubon_exec_smoke_entry_unknown_prints_diagnostics_without_close(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    set_smoke_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(
        entry_status=ExecutionOutcomeStatus.UNKNOWN,
        position_quantities=(0.0,),
    )
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
            "--raw-json",
        ]
    )

    exit_code = command_fubon_exec_smoke(args)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "entry_unknown_diagnostic: position=0, open_orders=0" in output
    assert "Fubon execution smoke order_records: count=2" in output
    assert "CRITICAL manual intervention required" in output
    assert len(fake_adapter.executed_plans) == 1
    assert fake_adapter.executed_plans[0].plan_type == ExecutionPlanType.ENTRY
    assert fake_adapter.close_called


def test_fubon_order_records_readonly_prints_position_and_records(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("LUX_READONLY_BROKER", "1")
    fake_adapter = FakeSmokeAdapter(position_quantities=(0.0,))
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-order-records",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--raw-json",
        ]
    )

    exit_code = command_fubon_order_records(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"Fubon order records: symbol={SYMBOL}" in output
    assert "Fubon execution smoke readonly: position=0, open_orders=0" in output
    assert "Fubon execution smoke order_records: count=2" in output
    assert "fake-entry" in output
    assert fake_adapter.executed_plans == []
    assert fake_adapter.close_called


def test_fubon_manual_close_requires_env_gates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clear_smoke_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-manual-close",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--side",
            "sell",
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="manual close gates closed"):
        command_fubon_manual_close(args)


def test_fubon_manual_close_rejects_confirm_symbol_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_manual_close_env(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-manual-close",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--side",
            "sell",
            "--lot",
            "1",
            "--confirm-symbol",
            "TMFH6",
        ]
    )

    with pytest.raises(SystemExit, match="confirm-symbol"):
        command_fubon_manual_close(args)


def test_fubon_manual_close_rejects_open_orders(
    tmp_path: Path,
    monkeypatch,
) -> None:
    set_manual_close_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(open_orders=({"id": "existing"},))
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-manual-close",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--side",
            "sell",
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    with pytest.raises(SystemExit, match="existing open orders"):
        command_fubon_manual_close(args)
    assert fake_adapter.executed_plans == []
    assert fake_adapter.close_called


def test_fubon_manual_close_sends_close_order_and_reports_success(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    set_manual_close_env(monkeypatch)
    fake_adapter = FakeSmokeAdapter(position_quantities=(1.0, 0.0))
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-manual-close",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--side",
            "sell",
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
            "--raw-json",
        ]
    )

    exit_code = command_fubon_manual_close(args)

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Fubon manual close complete" in output
    assert len(fake_adapter.executed_plans) == 1
    plan = fake_adapter.executed_plans[0]
    assert plan.plan_type == ExecutionPlanType.EXIT
    assert plan.legs[0].side == OrderSide.SELL
    assert fake_adapter.close_called


def test_fubon_manual_close_partial_fill_requires_manual_intervention(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    set_manual_close_env(monkeypatch)
    monkeypatch.setattr(cli_module.time, "sleep", lambda _: None)
    fake_adapter = FakeSmokeAdapter(
        position_quantities=(1.0, 1.0),
        exit_status=ExecutionOutcomeStatus.PARTIAL_FILL,
    )
    monkeypatch.setattr(
        cli_module,
        "FubonFutureExecutionAdapter",
        lambda *args, **kwargs: fake_adapter,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "fubon-manual-close",
            "--config",
            str(write_config(tmp_path)),
            "--symbol",
            SYMBOL,
            "--side",
            "sell",
            "--lot",
            "1",
            "--confirm-symbol",
            SYMBOL,
        ]
    )

    exit_code = command_fubon_manual_close(args)

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "CRITICAL manual intervention required" in output
    assert len(fake_adapter.executed_plans) == 1
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
