"""Real-execution CLI commands: live-execute, exec-smoke, manual-close, broker-status.

Consolidated from the legacy per-venue commands (`fubon-exec-smoke` /
`binance-exec-smoke` -> ``exec-smoke --venue``, `fubon-manual-close` /
`binance-manual-close` -> ``manual-close --venue``, `broker-doctor` /
`fubon-account-funds` / `fubon-order-records` -> ``broker-status``).
Bodies are behavior-preserving; only the command surface changed.

Everything here either sends REAL orders behind explicit config+env gates or
reads real accounts read-only. Tests monkeypatch the adapter names on this
module (e.g. ``commands_execution.FubonFutureExecutionAdapter``).
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

from lux_trader.cli import helpers
from lux_trader.config import load_config
from lux_trader.core.models import BrokerName, Direction, OrderSide
from lux_trader.execution.gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.integrations.binance.execution import (
    BINANCE_EXECUTION_SMOKE_ENV_GATES,
    BINANCE_MANUAL_CLOSE_ENV_GATES,
    BinanceTsmExecutionAdapter,
    binance_manual_close_env_gates_open,
    binance_smoke_env_gates_open,
)
from lux_trader.integrations.fubon.execution import (
    FUBON_EXECUTION_SMOKE_ENV_GATES,
    FUBON_MANUAL_CLOSE_ENV_GATES,
    FubonFutureExecutionAdapter,
    fubon_manual_close_env_gates_open,
    fubon_smoke_env_gates_open,
)
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.reconciliation import ReadOnlyBroker
from lux_trader.runtime.live import LiveExecuteRunner
from lux_trader.store import SQLiteStore


# ---------------------------------------------------------------------------
# live-execute + doctor --mode order
# ---------------------------------------------------------------------------


def build_live_execution_gate_report(config: object, store: SQLiteStore):
    return evaluate_live_execution_gate(
        config,
        reconciliation_report=store.load_latest_reconciliation_report(),
        now=datetime.now().astimezone(),
        include_plan_checks=False,
    )


def run_order_doctor_checks(config: object) -> tuple[bool, list[str]]:
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        report = build_live_execution_gate_report(config, store)
    finally:
        store.close()

    lines = [
        f"store_path={config.store_path}",
        "execution=real_two_leg_coordinator (qff_first)",
    ]
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"{status} {check.check_type}: {check.message}")
    return report.passed, lines


def command_live_execute(args: argparse.Namespace) -> int:
    from lux_trader.cli.commands_live import (
        build_live_reporter,
        reconcile_brokers_to_store,
    )

    config = load_config(args.config)
    reporter = build_live_reporter(args, config, mode="live-execute")
    try:
        store = SQLiteStore(config.store_path)
        try:
            if args.reset_store:
                store.reset()
            store.initialize()
            if config.live_execution.require_readonly_reconciliation:
                run_id, reconciliation_report = reconcile_brokers_to_store(
                    config,
                    store,
                    readonly=True,
                )
                reporter.event(
                    datetime.now().astimezone(),
                    "startup",
                    "readonly_reconciliation "
                    f"run_id={run_id} status={reconciliation_report.status.value}",
                )
            else:
                reconciliation_report = store.load_latest_reconciliation_report()
            gate_report = evaluate_live_execution_gate(
                config,
                reconciliation_report=reconciliation_report,
                include_plan_checks=False,
            )
        finally:
            store.close()
        assert_live_execution_gate_open(gate_report)
        LiveExecuteRunner(config, reporter=reporter).run(
            resume=args.resume,
            # A requested reset must happen before reconciliation so the fresh
            # report remains in the same store used by the live runtime.
            reset_store=False,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except Exception as exc:
        reporter.error(
            datetime.now().astimezone(), f"{type(exc).__name__}: {exc}"
        )
        if isinstance(exc, RuntimeError):
            raise SystemExit(str(exc))
        raise
    finally:
        reporter.finish()
    return 0


# ---------------------------------------------------------------------------
# exec-smoke --venue {fubon,binance}
# ---------------------------------------------------------------------------


def command_exec_smoke(args: argparse.Namespace) -> int:
    if args.venue == "binance":
        if args.quantity is None:
            raise SystemExit("--quantity is required for --venue binance")
        return binance_exec_smoke(args)
    if args.symbol is None:
        raise SystemExit("--symbol is required for --venue fubon")
    if args.lot is None:
        raise SystemExit("--lot is required for --venue fubon")
    return fubon_exec_smoke(args)


def binance_exec_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_binance_exec_smoke_ready(config, args)

    adapter = BinanceTsmExecutionAdapter(
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
        timestamp = datetime.now().astimezone().replace(microsecond=0)
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
            timestamp=datetime.now().astimezone().replace(microsecond=0),
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


def fubon_exec_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_fubon_exec_smoke_ready(config, args)

    symbol = str(args.symbol).strip()
    lot = int(args.lot)
    adapter = FubonFutureExecutionAdapter(
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

        timestamp = datetime.now().astimezone().replace(microsecond=0)
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
            timestamp=datetime.now().astimezone().replace(microsecond=0),
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


# ---------------------------------------------------------------------------
# manual-close --venue {fubon,binance}
# ---------------------------------------------------------------------------


def command_manual_close(args: argparse.Namespace) -> int:
    if args.venue == "binance":
        if args.quantity is None:
            raise SystemExit("--quantity is required for --venue binance")
        return binance_manual_close(args)
    if args.lot is None:
        raise SystemExit("--lot is required for --venue fubon")
    return fubon_manual_close(args)


def binance_manual_close(args: argparse.Namespace) -> int:
    """Emergency-close a Binance TSM position with a market order. Mirrors the
    Fubon path so either stranded leg from a single-leg PAUSE can be flattened
    by hand before clear-pause."""
    config = load_config(args.config)
    require_binance_manual_close_ready(config, args)

    symbol = str(args.symbol).strip()
    quantity = float(args.quantity)
    side = OrderSide.BUY if str(args.side).lower() == "buy" else OrderSide.SELL
    adapter = BinanceTsmExecutionAdapter(
        symbol,
        config.live.fubon_env_path,
        leverage=config.binance_execution.leverage,
        margin_mode=config.binance_execution.margin_mode,
        enforce_leverage=config.binance_execution.enforce_leverage,
    )
    try:
        pre_open_orders = adapter.fetch_open_orders()
        pre_position = adapter.fetch_position_quantity()
        print(
            "Binance manual close precheck: "
            f"symbol={symbol}, position={pre_position}, "
            f"open_orders={len(pre_open_orders)}"
        )
        if pre_open_orders:
            raise SystemExit(
                "Refusing Binance manual close with existing open orders: "
                f"{len(pre_open_orders)}"
            )
        if abs(pre_position) <= 1e-12:
            print(
                "WARN Binance position query returned zero before manual close; "
                "continuing because --side/--quantity were explicitly provided"
            )

        timestamp = datetime.now().astimezone().replace(microsecond=0)
        close_plan = build_binance_smoke_plan(
            symbol=symbol,
            quantity=quantity,
            side=side,
            plan_type=ExecutionPlanType.EXIT,
            timestamp=timestamp,
        )
        outcome = adapter.execute(close_plan)
        print_binance_smoke_outcome("manual_close", outcome)

        final_open_orders = adapter.fetch_open_orders()
        final_position = adapter.fetch_position_quantity()
        print(
            "Binance manual close after: "
            f"position={final_position}, open_orders={len(final_open_orders)}"
        )
        if not outcome.filled or final_open_orders or abs(final_position) > 1e-12:
            print("CRITICAL manual intervention required")
            return 1
        print(
            "Binance manual close complete: "
            f"position={final_position:g}, open_orders={len(final_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


def fubon_manual_close(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_fubon_manual_close_ready(config, args)

    symbol = str(args.symbol).strip()
    lot = int(args.lot)
    side = OrderSide.BUY if str(args.side).lower() == "buy" else OrderSide.SELL
    adapter = FubonFutureExecutionAdapter(
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

        timestamp = datetime.now().astimezone().replace(microsecond=0)
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


# ---------------------------------------------------------------------------
# broker-status (read-only: snapshots, funds, order records)
# ---------------------------------------------------------------------------


def command_broker_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order and not (args.funds or args.orders):
        raise SystemExit("allow_live_order must remain false for broker-status")

    if args.orders:
        return fubon_order_records_status(config, args)
    if args.funds:
        return fubon_account_funds_status(config, args)

    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_broker_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    checks = [
        f"store_path={config.store_path}",
        f"reconciliation_enabled={config.broker_reconciliation.enabled}",
        f"fail_on_mismatch={config.broker_reconciliation.fail_on_mismatch}",
        f"tsm_units_tolerance={config.broker_reconciliation.tsm_units_tolerance}",
        f"qff_contract_tolerance={config.broker_reconciliation.qff_contract_tolerance}",
        "private_api=disabled",
    ]
    brokers: tuple[ReadOnlyBroker, ...] = ()
    if helpers.readonly_broker_enabled():
        brokers = helpers.build_real_readonly_brokers(config)
        checks[-1] = "private_api=readonly"
        try:
            for broker in brokers:
                snapshot = broker.fetch_snapshot()
                checks.append(
                    f"{snapshot.broker.value}_snapshot="
                    f"account={snapshot.account_id} "
                    f"positions={len(snapshot.positions)} "
                    f"open_orders={len(snapshot.open_orders)} "
                    f"margins={len(snapshot.margins)}"
                )
        finally:
            helpers.close_brokers(brokers)
    print("Broker status checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def fubon_account_funds_status(config: object, args: argparse.Namespace) -> int:
    if not helpers.readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to query Fubon account funds")

    broker = FubonReadOnlyBroker(config.live.fubon_env_path)
    try:
        snapshot = broker.fetch_snapshot()
    finally:
        broker.close()

    print("Fubon account funds")
    print(f"- account={snapshot.account_id}")
    print(f"- fetched_at={snapshot.fetched_at.isoformat()}")
    print(f"- positions={len(snapshot.positions)}")
    print(f"- open_orders={len(snapshot.open_orders)}")
    if not snapshot.margins:
        print("- margins=0")
        return 0

    print(f"- margins={len(snapshot.margins)}")
    for index, margin in enumerate(snapshot.margins, start=1):
        print(
            f"- margin[{index}] "
            f"currency={margin.currency} "
            f"equity={format_optional_number(margin.equity)} "
            f"available={format_optional_number(margin.available)} "
            f"margin_used={format_optional_number(margin.margin_used)}"
        )
        if args.raw_json:
            print(
                json.dumps(
                    margin.raw or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    default=str,
                )
            )
    return 0


def fubon_order_records_status(config: object, args: argparse.Namespace) -> int:
    if not helpers.readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to query Fubon order records")
    symbol = str(args.orders).strip()
    if not symbol:
        raise SystemExit("--orders requires a symbol")

    adapter = FubonFutureExecutionAdapter(
        symbol,
        config.live.fubon_env_path,
    )
    try:
        position = adapter.fetch_position_quantity()
        open_orders = adapter.fetch_open_orders()
        records = adapter.fetch_order_records()
    finally:
        adapter.close()

    print(f"Fubon order records: symbol={symbol}")
    print_fubon_smoke_position(
        "readonly",
        position=position,
        open_orders=len(open_orders),
    )
    print_fubon_smoke_order_records(records, raw_json=bool(args.raw_json))
    return 0


def format_optional_number(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# gate/require helpers
# ---------------------------------------------------------------------------


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


def require_fubon_exec_smoke_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    if not config.live_execution.enabled:
        raise SystemExit("live_execution.enabled=true is required")
    symbol = str(args.symbol or "").strip()
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


def require_binance_manual_close_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    symbol = str(args.symbol or "").strip()
    if not symbol:
        raise SystemExit("--symbol is required")
    if str(args.confirm_symbol).strip() != symbol:
        raise SystemExit("--confirm-symbol must match --symbol")
    if float(args.quantity) <= 0.0:
        raise SystemExit("--quantity must be positive")
    gates = binance_manual_close_env_gates_open()
    closed = [name for name in BINANCE_MANUAL_CLOSE_ENV_GATES if not gates[name]]
    if closed:
        raise SystemExit(
            "Binance manual close gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def require_fubon_manual_close_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    symbol = str(args.symbol or "").strip()
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


# ---------------------------------------------------------------------------
# plan builders / printers / retry
# ---------------------------------------------------------------------------


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
            time.sleep(interval_seconds)
    return last


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


def print_binance_smoke_outcome(label: str, outcome: object) -> None:
    filled_quantity = sum(fill.quantity for fill in getattr(outcome, "fills", ()))
    print(
        "Binance execution smoke "
        f"{label}: status={outcome.status.value}, "
        f"fills={len(outcome.fills)}, filled_quantity={filled_quantity}, "
        f"message={outcome.message}"
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
