from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy
import pandas

from lux_trader.integrations.binance.execution import (
    BINANCE_EXECUTION_SMOKE_ENV_GATES,
    BinanceTsmExecutionAdapter,
    binance_smoke_env_gates_open,
)
from lux_trader.core.calendar import live_session_status
from lux_trader.config import load_config
from lux_trader.cli_helpers import (
    build_fake_execution_plan,
    build_reconciliation_brokers,
    build_real_readonly_brokers,
    close_brokers,
    readonly_broker_enabled,
    reconciliation_qff_symbol,
)
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
    pair_execution_plan_from_jsonable,
)
from lux_trader.execution.recorder import DryRunExecutionRecorder
from lux_trader.execution.simulation import DryRunExecutionSimulator, ExecutionSimulationScenario
from lux_trader.integrations.fubon.execution import (
    FUBON_EXECUTION_SMOKE_ENV_GATES,
    FUBON_MANUAL_CLOSE_ENV_GATES,
    FubonFutureExecutionAdapter,
    fubon_manual_close_env_gates_open,
    fubon_smoke_env_gates_open,
)
from lux_trader.execution.gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from lux_trader.core.time import ensure_taipei
from lux_trader.integrations.binance.market_data import BinanceMarketData
from lux_trader.integrations.bitopro.market_data import BitoProMarketData
from lux_trader.integrations.fubon.market_data import FubonQffMarketData
from lux_trader.runtime.live import (
    LiveDryRunRunner,
    LiveExecuteRunner,
    LivePaperRunner,
    QffWarmupCheckRunner,
    WarmupRunner,
    resolve_qff_contract,
)
from lux_trader.reconciliation import (
    BrokerReconciler,
    ReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.core.models import BrokerName, Direction, OrderSide
from lux_trader.runner import SystemRunner
from lux_trader.store import SQLiteStore
from lux_trader.terminal_ui import LiveTerminalReporter, NullLiveReporter

from lux_trader.cli.compat import cli_attr


LIVE_MARKETDATA_ENV = "LUX_LIVE_MARKETDATA"
LIVE_MARKETDATA_DEFAULT = "1"


def command_fubon_manual_close(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_fubon_manual_close_ready(config, args)

    symbol = str(args.symbol).strip()
    lot = int(args.lot)
    side = OrderSide.BUY if str(args.side).lower() == "buy" else OrderSide.SELL
    adapter = cli_attr("FubonFutureExecutionAdapter", FubonFutureExecutionAdapter)(
        symbol,
        config.live.fubon_env_path,
    )
    try:
        pre_position = adapter.fetch_position_quantity()
        pre_open_orders = adapter.fetch_open_orders()
        print_fubon_smoke_position(
            "manual_close_precheck",
            position=pre_position,
            open_orders=len(pre_open_orders),
        )
        print_fubon_smoke_order_records(
            adapter.fetch_order_records(),
            raw_json=bool(args.raw_json),
        )
        if pre_open_orders:
            raise SystemExit(
                "Refusing Fubon manual close with existing open orders: "
                f"{len(pre_open_orders)}"
            )
        if abs(pre_position) <= 1e-12:
            print(
                "WARN Fubon position query returned zero before manual close; "
                "continuing because --side/--lot were explicitly provided"
            )

        timestamp = cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0)
        close_plan = build_fubon_smoke_plan(
            symbol=symbol,
            lot=lot,
            side=side,
            plan_type=ExecutionPlanType.EXIT,
            timestamp=timestamp,
        )
        outcome = adapter.execute(close_plan)
        print_fubon_smoke_outcome("manual_close", outcome)

        final_open_orders = adapter.fetch_open_orders()
        final_position = fetch_fubon_position_with_retry(
            adapter,
            expected="zero",
        )
        print_fubon_smoke_position(
            "manual_close_after",
            position=final_position,
            open_orders=len(final_open_orders),
        )
        print_fubon_smoke_order_records(
            adapter.fetch_order_records(),
            raw_json=bool(args.raw_json),
        )
        if not outcome.filled or final_open_orders or abs(final_position) > 1e-12:
            print("CRITICAL manual intervention required")
            return 1
        print(
            "Fubon manual close complete: "
            f"position={final_position:g}, open_orders={len(final_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


def command_dry_run_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for dry-run-doctor")
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        summary = store.build_execution_summary()
    finally:
        store.close()
    checks = [
        f"store_path={config.store_path}",
        f"execution_plans={summary['plan_count']}",
        f"execution_legs={summary['leg_count']}",
        f"execution_checks={summary['check_count']}",
        f"live_order={config.safety.allow_live_order}",
        "private_api=disabled",
    ]
    print("Dry-run doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def command_execution_summary(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        print(json.dumps(store.build_execution_summary(), indent=2))
    finally:
        store.close()
    return 0


def command_live_dry_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for live-dry-run")
    if not args.fake:
        reporter = (
            NullLiveReporter()
            if args.quiet_ui
            else LiveTerminalReporter(color=False if args.no_color else None)
        )
        try:
            result = LiveDryRunRunner(config, reporter=reporter).run(
                resume=args.resume,
                reset_store=args.reset_store,
                max_iterations=args.max_iterations,
                skip_warmup=args.skip_warmup,
            )
        except Exception as exc:
            reporter.error(cli_attr("datetime", datetime).now().astimezone(), f"{type(exc).__name__}: {exc}")
            raise
        reporter.finish()
        print(
            "Live dry-run stopped: "
            f"iterations={result.iterations}, "
            f"bars_processed={result.bars_processed}, "
            f"skipped_minutes={result.skipped_minutes}, "
            f"plans_recorded={result.plans_recorded}, "
            f"qff_symbol={result.qff_symbol}"
        )
        return 0
    if args.max_bars is not None and args.max_bars < 1:
        raise SystemExit("--max-bars must be >= 1")

    store = SQLiteStore(config.store_path)
    recorded_plans = []
    try:
        if args.reset_store:
            store.reset()
        store.initialize()
        recorder = DryRunExecutionRecorder(
            store,
            allow_live_order=config.safety.allow_live_order,
        )
        timestamp = cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0)
        max_bars = int(args.max_bars or 1)
        for index in range(max_bars):
            plan = build_fake_execution_plan(
                config,
                fake_case=args.fake_case,
                timestamp=timestamp + timedelta(minutes=index),
                row_index=index,
            )
            recorded_plans.append(recorder.record_plan(plan))
        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        store.close()

    failed = [plan for plan in recorded_plans if not plan.accepted]
    latest_status = recorded_plans[-1].status.value if recorded_plans else "none"
    print(
        "Live dry-run skeleton complete: "
        f"plans={len(recorded_plans)}, "
        f"status={latest_status}, "
        f"failed={len(failed)}"
    )
    for plan in recorded_plans:
        print(
            f"- {plan.plan_id} {plan.plan_type.value} "
            f"{plan.direction.value} status={plan.status.value} "
            f"failed_checks={sum(1 for check in plan.checks if not check.passed)}"
        )
    return 1 if failed else 0


def command_simulate_execution(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for simulate-execution")

    store = SQLiteStore(config.store_path)
    try:
        if args.reset_store:
            store.reset()
        store.initialize()
        if args.fake_plan:
            plan = DryRunExecutionRecorder(
                store,
                allow_live_order=config.safety.allow_live_order,
            ).record_plan(
                build_fake_execution_plan(
                    config,
                    fake_case="valid",
                    timestamp=cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0),
                    row_index=0,
                )
            )
        else:
            payload = store.load_latest_execution_plan_payload()
            if payload is None:
                raise SystemExit(
                    "No execution plan found. Use --fake-plan or run live-dry-run first."
                )
            plan = pair_execution_plan_from_jsonable(payload)
        result = DryRunExecutionSimulator().simulate(plan, args.scenario)
        simulation_id = store.record_execution_simulation(result)
        store.record_event(
            plan.row_index,
            result.timestamp,
            "execution_simulation",
            result.message,
            result.to_jsonable(),
        )
        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        store.close()

    print(
        "Execution simulation complete: "
        f"simulation_id={simulation_id}, "
        f"plan_id={result.plan_id}, "
        f"scenario={result.scenario.value}, "
        f"status={result.status.value}, "
        f"recommended_state={result.recommended_state}"
    )
    return 0


def command_live_order_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        report = build_live_execution_gate_report(config, store)
    finally:
        store.close()

    print(f"Live execution gate status={'open' if report.passed else 'closed'}")
    print(f"store_path={config.store_path}")
    print("phase5_adapter=real_execution_coordinator")
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"- {status} {check.check_type}: {check.message}")
    return 0


def command_live_execute(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    reporter = (
        NullLiveReporter()
        if args.quiet_ui
        else LiveTerminalReporter(color=False if args.no_color else None)
    )
    try:
        store = SQLiteStore(config.store_path)
        try:
            store.initialize()
            gate_report = evaluate_live_execution_gate(
                config,
                reconciliation_report=store.load_latest_reconciliation_report(),
                include_plan_checks=False,
            )
        finally:
            store.close()
        assert_live_execution_gate_open(gate_report)
        cli_attr("LiveExecuteRunner", LiveExecuteRunner)(config, reporter=reporter).run(
            resume=args.resume,
            reset_store=args.reset_store,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except RuntimeError as exc:
        reporter.error(cli_attr("datetime", datetime).now().astimezone(), f"{type(exc).__name__}: {exc}")
        raise SystemExit(str(exc))
    finally:
        reporter.finish()
    return 0


def command_binance_exec_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_binance_exec_smoke_ready(config, args)

    adapter = cli_attr("BinanceTsmExecutionAdapter", BinanceTsmExecutionAdapter)(
        config.live.binance_symbol,
        config.live.fubon_env_path,
        leverage=config.binance_execution.leverage,
        margin_mode=config.binance_execution.margin_mode,
        enforce_leverage=config.binance_execution.enforce_leverage,
    )
    try:
        preflight = adapter.preflight()
        if preflight.open_orders:
            raise SystemExit(
                "Refusing Binance execution smoke with existing open orders: "
                f"{len(preflight.open_orders)}"
            )
        if abs(preflight.position_quantity) > 1e-12:
            raise SystemExit(
                "Refusing Binance execution smoke with nonzero position: "
                f"{preflight.position_quantity}"
            )

        quantity = float(args.quantity)
        timestamp = cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0)
        print(
            "Binance execution smoke preflight passed: "
            f"symbol={config.live.binance_symbol}, quantity={quantity}, "
            f"margin_mode={config.binance_execution.margin_mode}, "
            f"leverage={config.binance_execution.leverage}, "
            f"enforce_leverage={config.binance_execution.enforce_leverage}"
        )

        entry_plan = build_binance_smoke_plan(
            symbol=config.live.binance_symbol,
            quantity=quantity,
            side=OrderSide.BUY,
            plan_type=ExecutionPlanType.ENTRY,
            timestamp=timestamp,
        )
        entry_outcome = adapter.execute(entry_plan)
        print_binance_smoke_outcome("entry", entry_outcome)
        entry_filled_quantity = sum(fill.quantity for fill in entry_outcome.fills)
        if entry_filled_quantity <= 0:
            return 1

        exit_plan = build_binance_smoke_plan(
            symbol=config.live.binance_symbol,
            quantity=entry_filled_quantity,
            side=OrderSide.SELL,
            plan_type=ExecutionPlanType.EXIT,
            timestamp=cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0),
        )
        exit_outcome = adapter.execute(exit_plan)
        print_binance_smoke_outcome("exit", exit_outcome)

        final_open_orders = adapter.fetch_open_orders()
        final_position = adapter.fetch_position_quantity()
        if (
            not entry_outcome.filled
            or not exit_outcome.filled
            or final_open_orders
            or abs(final_position) > 1e-12
        ):
            print("CRITICAL manual intervention required")
            print(
                "Binance execution smoke final state: "
                f"position={final_position}, open_orders={len(final_open_orders)}"
            )
            return 1

        print(
            "Binance execution smoke complete: "
            f"position={final_position}, open_orders={len(final_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


def require_binance_exec_smoke_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    if not config.live_execution.enabled:
        raise SystemExit("live_execution.enabled=true is required")
    if str(args.confirm_symbol).strip() != config.live.binance_symbol:
        raise SystemExit(
            "--confirm-symbol must match config live_market_data.binance_symbol"
        )
    if float(args.quantity) <= 0.0:
        raise SystemExit("--quantity must be positive")
    gates = binance_smoke_env_gates_open()
    closed = [name for name in BINANCE_EXECUTION_SMOKE_ENV_GATES if not gates[name]]
    if closed:
        raise SystemExit(
            "Binance execution smoke gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def build_binance_smoke_plan(
    *,
    symbol: str,
    quantity: float,
    side: OrderSide,
    plan_type: ExecutionPlanType,
    timestamp: datetime,
) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id=f"BINANCE-SMOKE-{timestamp.strftime('%Y%m%d%H%M%S')}-{plan_type.value}",
        plan_type=plan_type,
        direction=Direction.LONG_TSM_SHORT_QFF,
        timestamp=timestamp,
        row_index=-1,
        legs=(
            ExecutionLeg(
                broker=BrokerName.BINANCE_TSM,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=1.0,
                timestamp=timestamp,
                row_index=-1,
            ),
        ),
        reason="binance_execution_smoke",
        decision_spread_type="manual_smoke",
    )


def print_binance_smoke_outcome(label: str, outcome: object) -> None:
    filled_quantity = sum(fill.quantity for fill in getattr(outcome, "fills", ()))
    print(
        "Binance execution smoke "
        f"{label}: status={outcome.status.value}, "
        f"fills={len(outcome.fills)}, filled_quantity={filled_quantity}, "
        f"message={outcome.message}"
    )


def command_fubon_exec_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_fubon_exec_smoke_ready(config, args)

    symbol = str(args.symbol).strip()
    lot = int(args.lot)
    adapter = cli_attr("FubonFutureExecutionAdapter", FubonFutureExecutionAdapter)(
        symbol,
        config.live.fubon_env_path,
    )
    try:
        preflight = adapter.preflight()
        if preflight.open_orders:
            raise SystemExit(
                "Refusing Fubon execution smoke with existing open orders: "
                f"{len(preflight.open_orders)}"
            )
        if abs(preflight.position_quantity) > 1e-12:
            raise SystemExit(
                "Refusing Fubon execution smoke with nonzero position: "
                f"{preflight.position_quantity}"
            )

        timestamp = cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0)
        print(
            "Fubon execution smoke preflight passed: "
            f"symbol={symbol}, lot={lot}"
        )

        entry_plan = build_fubon_smoke_plan(
            symbol=symbol,
            lot=lot,
            side=OrderSide.BUY,
            plan_type=ExecutionPlanType.ENTRY,
            timestamp=timestamp,
        )
        entry_outcome = adapter.execute(entry_plan)
        print_fubon_smoke_outcome("entry", entry_outcome)
        entry_filled_lot = int(sum(fill.quantity for fill in entry_outcome.fills))
        if entry_filled_lot <= 0:
            diagnostic_position = adapter.fetch_position_quantity()
            diagnostic_open_orders = adapter.fetch_open_orders()
            print_fubon_smoke_position(
                "entry_unknown_diagnostic",
                position=diagnostic_position,
                open_orders=len(diagnostic_open_orders),
            )
            print_fubon_smoke_order_records(
                adapter.fetch_order_records(),
                raw_json=bool(args.raw_json),
            )
            print("CRITICAL manual intervention required")
            return 1
        after_entry_position = fetch_fubon_position_with_retry(
            adapter,
            expected="nonzero",
        )
        after_entry_open_orders = adapter.fetch_open_orders()
        print_fubon_smoke_position(
            "after_entry",
            position=after_entry_position,
            open_orders=len(after_entry_open_orders),
        )
        if abs(after_entry_position) <= 1e-12:
            print(
                "WARN Fubon position query returned zero after a filled entry; "
                "continuing with close attempt because entry fill was reported"
            )

        exit_plan = build_fubon_smoke_plan(
            symbol=symbol,
            lot=entry_filled_lot,
            side=OrderSide.SELL,
            plan_type=ExecutionPlanType.EXIT,
            timestamp=cli_attr("datetime", datetime).now().astimezone().replace(microsecond=0),
        )
        exit_outcome = adapter.execute(exit_plan)
        print_fubon_smoke_outcome("exit", exit_outcome)

        final_open_orders = adapter.fetch_open_orders()
        final_position = fetch_fubon_position_with_retry(
            adapter,
            expected="zero",
        )
        print_fubon_smoke_position(
            "after_exit",
            position=final_position,
            open_orders=len(final_open_orders),
        )
        print_fubon_smoke_order_records(
            adapter.fetch_order_records(),
            raw_json=bool(args.raw_json),
        )
        if (
            not entry_outcome.filled
            or not exit_outcome.filled
            or final_open_orders
            or abs(final_position) > 1e-12
        ):
            print("CRITICAL manual intervention required")
            print(
                "Fubon execution smoke final state: "
                f"position={final_position}, open_orders={len(final_open_orders)}"
            )
            return 1

        print(
            "Fubon execution smoke complete: "
            f"position={final_position}, open_orders={len(final_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


def require_fubon_exec_smoke_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    if not config.live_execution.enabled:
        raise SystemExit("live_execution.enabled=true is required")
    symbol = str(args.symbol).strip()
    if not symbol:
        raise SystemExit("--symbol is required")
    if str(args.confirm_symbol).strip() != symbol:
        raise SystemExit("--confirm-symbol must match --symbol")
    if int(args.lot) <= 0:
        raise SystemExit("--lot must be positive")
    gates = fubon_smoke_env_gates_open()
    closed = [name for name in FUBON_EXECUTION_SMOKE_ENV_GATES if not gates[name]]
    if closed:
        raise SystemExit(
            "Fubon execution smoke gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def require_fubon_manual_close_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    symbol = str(args.symbol).strip()
    if not symbol:
        raise SystemExit("--symbol is required")
    if str(args.confirm_symbol).strip() != symbol:
        raise SystemExit("--confirm-symbol must match --symbol")
    if int(args.lot) <= 0:
        raise SystemExit("--lot must be positive")
    gates = fubon_manual_close_env_gates_open()
    closed = [name for name in FUBON_MANUAL_CLOSE_ENV_GATES if not gates[name]]
    if closed:
        raise SystemExit(
            "Fubon manual close gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def fetch_fubon_position_with_retry(
    adapter: object,
    *,
    expected: str,
    attempts: int = 5,
    interval_seconds: float = 0.5,
) -> float:
    last = 0.0
    for attempt in range(max(1, attempts)):
        last = float(adapter.fetch_position_quantity())
        if expected == "nonzero" and abs(last) > 1e-12:
            return last
        if expected == "zero" and abs(last) <= 1e-12:
            return last
        if attempt < attempts - 1 and interval_seconds > 0:
            cli_attr("time", time).sleep(interval_seconds)
    return last


def build_fubon_smoke_plan(
    *,
    symbol: str,
    lot: int,
    side: OrderSide,
    plan_type: ExecutionPlanType,
    timestamp: datetime,
) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id=f"FUBON-SMOKE-{timestamp.strftime('%Y%m%d%H%M%S')}-{plan_type.value}",
        plan_type=plan_type,
        direction=Direction.SHORT_TSM_LONG_QFF,
        timestamp=timestamp,
        row_index=-1,
        legs=(
            ExecutionLeg(
                broker=BrokerName.FUBON_QFF,
                symbol=symbol,
                side=side,
                quantity=float(lot),
                price=1.0,
                timestamp=timestamp,
                row_index=-1,
                qff_symbol=symbol,
            ),
        ),
        reason="fubon_execution_smoke",
        decision_spread_type="manual_smoke",
        qff_symbol=symbol,
    )


def print_fubon_smoke_outcome(label: str, outcome: object) -> None:
    filled_lot = sum(fill.quantity for fill in getattr(outcome, "fills", ()))
    print(
        "Fubon execution smoke "
        f"{label}: status={outcome.status.value}, "
        f"fills={len(outcome.fills)}, filled_lot={filled_lot:g}, "
        f"message={outcome.message}"
    )


def print_fubon_smoke_position(
    label: str,
    *,
    position: float,
    open_orders: int,
) -> None:
    print(
        "Fubon execution smoke "
        f"{label}: position={position:g}, open_orders={open_orders}"
    )


def print_fubon_smoke_order_records(
    records: tuple[dict[str, object], ...],
    *,
    raw_json: bool,
) -> None:
    print(f"Fubon execution smoke order_records: count={len(records)}")
    for index, record in enumerate(records, start=1):
        print(
            f"- record[{index}] "
            f"order_id={fubon_record_first(record, 'order_no', 'orderNo', 'ord_no', 'seq_no', 'seqNo', 'id') or 'UNKNOWN'} "
            f"symbol={fubon_record_first(record, 'symbol', 'code', 'prod_id', 'stock_no') or 'UNKNOWN'} "
            f"side={fubon_record_first(record, 'buy_sell', 'buySell', 'bs', 'side') or 'UNKNOWN'} "
            f"status={fubon_record_first(record, 'status', 'order_status', 'orderStatus', 'state') or 'UNKNOWN'} "
            f"lot={fubon_record_first(record, 'lot', 'lots', 'quantity', 'qty') or 'NA'} "
            f"filled={fubon_record_first(record, 'filled_lot', 'filledLot', 'match_lot', 'matchLot', 'deal_lot', 'dealLot', 'filled') or 'NA'} "
            f"avg_price={fubon_record_avg_price(record) or 'NA'}"
        )
        if raw_json:
            print(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    default=str,
                )
            )


def fubon_record_first(record: dict[str, object], *names: str) -> object | None:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
        lowered = name.lower()
        for key, value in record.items():
            if str(key).lower() == lowered and value not in (None, ""):
                return value
    return None


def fubon_record_avg_price(record: dict[str, object]) -> object | None:
    direct = fubon_record_first(
        record,
        "average_price",
        "averagePrice",
        "avg_price",
        "avgPrice",
        "match_price",
        "matchPrice",
        "deal_price",
        "dealPrice",
    )
    if direct not in (None, ""):
        return direct
    filled_money = parse_cli_float(
        fubon_record_first(record, "filled_money", "filledMoney")
    )
    filled_lot = parse_cli_float(
        fubon_record_first(record, "filled_lot", "filledLot", "deal_lot", "dealLot")
    )
    if filled_money is None or filled_lot is None or filled_lot == 0:
        return None
    price = filled_money / filled_lot
    if price.is_integer():
        return int(price)
    return price


def parse_cli_float(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_live_execution_gate_report(config: object, store: SQLiteStore):
    return evaluate_live_execution_gate(
        config,
        reconciliation_report=store.load_latest_reconciliation_report(),
        now=cli_attr("datetime", datetime).now().astimezone(),
        include_plan_checks=False,
    )

