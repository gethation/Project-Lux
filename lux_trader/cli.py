from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy
import pandas

from .binance_execution import (
    BINANCE_EXECUTION_SMOKE_ENV_GATES,
    BinanceTsmExecutionAdapter,
    binance_smoke_env_gates_open,
)
from .calendar import live_session_status
from .config import load_config
from .cli_helpers import (
    build_fake_execution_plan,
    build_reconciliation_brokers,
    build_real_readonly_brokers,
    close_brokers,
    readonly_broker_enabled,
    reconciliation_qff_symbol,
)
from .execution_intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
    pair_execution_plan_from_jsonable,
)
from .execution_recorder import DryRunExecutionRecorder
from .execution_simulator import DryRunExecutionSimulator, ExecutionSimulationScenario
from .fubon_execution import (
    FUBON_EXECUTION_SMOKE_ENV_GATES,
    FubonFutureExecutionAdapter,
    fubon_smoke_env_gates_open,
)
from .live_execution_gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from .live_market_data import CcxtTickerMarketData, FubonQffMarketData, ensure_taipei
from .live_runner import (
    LiveDryRunRunner,
    LiveExecuteRunner,
    LivePaperRunner,
    QffWarmupCheckRunner,
    WarmupRunner,
    resolve_qff_contract,
)
from .reconciliation import (
    BrokerReconciler,
    ReadOnlyBroker,
    ReconciliationStatus,
)
from .models import BrokerName, Direction, OrderSide
from .runner import SystemRunner
from .store import SQLiteStore
from .terminal_ui import LiveTerminalReporter, NullLiveReporter


LIVE_MARKETDATA_ENV = "LUX_LIVE_MARKETDATA"
LIVE_MARKETDATA_DEFAULT = "1"


def live_marketdata_enabled() -> bool:
    return os.getenv(LIVE_MARKETDATA_ENV, LIVE_MARKETDATA_DEFAULT).strip() == "1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lux_trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="Run CSV replay into SQLite")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--max-bars", type=int)
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--reset-store", action="store_true")

    summary = subparsers.add_parser("summary", help="Print SQLite replay summary")
    summary.add_argument("--config", type=Path, required=True)

    doctor = subparsers.add_parser("doctor", help="Check MVP configuration")
    doctor.add_argument("--config", type=Path, required=True)

    broker_doctor = subparsers.add_parser(
        "broker-doctor",
        help="Check read-only broker reconciliation skeleton config",
    )
    broker_doctor.add_argument("--config", type=Path, required=True)

    reconcile_brokers = subparsers.add_parser(
        "reconcile-brokers",
        help="Run read-only broker/store reconciliation",
    )
    reconcile_brokers.add_argument("--config", type=Path, required=True)
    reconcile_brokers.add_argument(
        "--fake",
        action="store_true",
        help="Use deterministic fake read-only brokers",
    )
    reconcile_brokers.add_argument(
        "--readonly",
        action="store_true",
        help="Use real Fubon and Binance read-only brokers",
    )
    reconcile_brokers.add_argument(
        "--fubon-readonly",
        action="store_true",
        help="Use real Fubon read-only broker",
    )
    reconcile_brokers.add_argument(
        "--fake-binance",
        action="store_true",
        help="Use fake Binance broker with real Fubon",
    )
    reconcile_brokers.add_argument(
        "--fake-case",
        choices=("matched", "mismatch", "error"),
        default="matched",
        help="Fake broker scenario for the Phase 3 skeleton",
    )

    dry_run_doctor = subparsers.add_parser(
        "dry-run-doctor",
        help="Check dry-run execution recorder skeleton config",
    )
    dry_run_doctor.add_argument("--config", type=Path, required=True)

    execution_summary = subparsers.add_parser(
        "execution-summary",
        help="Print dry-run execution intent summary",
    )
    execution_summary.add_argument("--config", type=Path, required=True)

    live_dry_run = subparsers.add_parser(
        "live-dry-run",
        help="Run dry-run execution intent skeleton",
    )
    live_dry_run.add_argument("--config", type=Path, required=True)
    live_dry_run.add_argument("--resume", action="store_true")
    live_dry_run.add_argument("--reset-store", action="store_true")
    live_dry_run.add_argument("--max-iterations", type=int)
    live_dry_run.add_argument(
        "--fake",
        action="store_true",
        help="Use deterministic fake execution intents",
    )
    live_dry_run.add_argument(
        "--fake-case",
        choices=("valid", "rejected"),
        default="valid",
        help="Fake dry-run scenario for the Phase 4 skeleton",
    )
    live_dry_run.add_argument("--max-bars", type=int, default=1)
    live_dry_run.add_argument(
        "--quiet-ui",
        action="store_true",
        help="Disable live terminal UI and print only the final summary",
    )
    live_dry_run.add_argument(
        "--no-color",
        action="store_true",
        help="Keep live terminal UI but disable ANSI colors",
    )
    live_dry_run.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them at startup",
    )

    simulate_execution = subparsers.add_parser(
        "simulate-execution",
        help="Simulate dry-run execution failure scenarios",
    )
    simulate_execution.add_argument("--config", type=Path, required=True)
    simulate_execution.add_argument(
        "--scenario",
        choices=tuple(scenario.value for scenario in ExecutionSimulationScenario),
        required=True,
    )
    simulate_execution.add_argument(
        "--fake-plan",
        action="store_true",
        help="Create a deterministic fake execution plan before simulating",
    )
    simulate_execution.add_argument("--reset-store", action="store_true")

    live_order_doctor = subparsers.add_parser(
        "live-order-doctor",
        help="Check Phase 5 live execution gates without sending orders",
    )
    live_order_doctor.add_argument("--config", type=Path, required=True)

    live_execute = subparsers.add_parser(
        "live-execute",
        help="Reserved Phase 5 live execution entrypoint",
    )
    live_execute.add_argument("--config", type=Path, required=True)
    live_execute.add_argument("--resume", action="store_true")
    live_execute.add_argument("--reset-store", action="store_true")
    live_execute.add_argument("--max-iterations", type=int)
    live_execute.add_argument(
        "--quiet-ui",
        action="store_true",
        help="Disable live terminal UI and print only the final summary",
    )
    live_execute.add_argument(
        "--no-color",
        action="store_true",
        help="Keep live terminal UI but disable ANSI colors",
    )
    live_execute.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them at startup",
    )

    binance_exec_smoke = subparsers.add_parser(
        "binance-exec-smoke",
        help="Manually run a tiny Binance TSM execution adapter smoke",
    )
    binance_exec_smoke.add_argument("--config", type=Path, required=True)
    binance_exec_smoke.add_argument("--quantity", type=float, required=True)
    binance_exec_smoke.add_argument("--confirm-symbol", required=True)

    fubon_exec_smoke = subparsers.add_parser(
        "fubon-exec-smoke",
        help="Manually run a tiny Fubon futures execution adapter smoke",
    )
    fubon_exec_smoke.add_argument("--config", type=Path, required=True)
    fubon_exec_smoke.add_argument("--symbol", required=True)
    fubon_exec_smoke.add_argument("--lot", type=int, required=True)
    fubon_exec_smoke.add_argument("--confirm-symbol", required=True)

    live_doctor = subparsers.add_parser("live-doctor", help="Check live-paper config")
    live_doctor.add_argument("--config", type=Path, required=True)

    warmup_live = subparsers.add_parser("warmup-live", help="Seed live-paper warmup bars")
    warmup_live.add_argument("--config", type=Path, required=True)
    warmup_live.add_argument("--reset-store", action="store_true")

    qff_warmup_check = subparsers.add_parser(
        "qff-warmup-check",
        help="Validate Fubon + TAIFEX QFF warmup data",
    )
    qff_warmup_check.add_argument("--config", type=Path, required=True)
    qff_warmup_check.add_argument(
        "--output-csv",
        default=None,
        help="Write comparison CSV to this path; pass an empty string to disable",
    )

    live_paper = subparsers.add_parser("live-paper", help="Run live market data with PaperBroker")
    live_paper.add_argument("--config", type=Path, required=True)
    live_paper.add_argument("--resume", action="store_true")
    live_paper.add_argument("--reset-store", action="store_true")
    live_paper.add_argument("--max-iterations", type=int)
    live_paper.add_argument(
        "--quiet-ui",
        action="store_true",
        help="Disable live terminal UI and print only the final summary",
    )
    live_paper.add_argument(
        "--no-color",
        action="store_true",
        help="Keep live terminal UI but disable ANSI colors",
    )
    live_paper.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them at startup",
    )
    return parser


def command_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("Refusing to run MVP replay with allow_live_order=true")
    result = SystemRunner(config).replay(
        max_bars=args.max_bars,
        resume=args.resume,
        reset_store=args.reset_store,
    )
    print(
        "Replay complete: "
        f"rows_processed={result.rows_processed}, "
        f"start_row={result.start_row}, "
        f"end_row={result.end_row}, "
        f"finalized={result.finalized}"
    )
    return 0


def command_summary(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        print(json.dumps(store.build_summary(config.strategy, config.fees), indent=2))
    finally:
        store.close()
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    checks: list[str] = []
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for this MVP")
    if not config.input_csv.exists():
        raise SystemExit(f"Input CSV does not exist: {config.input_csv}")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    checks.append(f"input_csv={config.input_csv}")
    checks.append(f"store_path={config.store_path}")
    checks.append(f"python={sys.version.split()[0]}")
    checks.append(f"pandas={pandas.__version__}")
    checks.append(f"numpy={numpy.__version__}")
    checks.append(f"live_order={config.safety.allow_live_order}")
    print("Doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def command_broker_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for broker-doctor")
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
    if readonly_broker_enabled():
        brokers = build_real_readonly_brokers(config)
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
            close_brokers(brokers)
    print("Broker doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def command_reconcile_brokers(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for reconcile-brokers")

    store = SQLiteStore(config.store_path)
    brokers: tuple[ReadOnlyBroker, ...] = ()
    try:
        store.initialize()
        resume_state = store.load_resume_state()
        strategy_state = resume_state.strategy if resume_state is not None else None
        timestamp = datetime.now().astimezone()
        brokers = build_reconciliation_brokers(
            args,
            config,
            strategy_state,
            timestamp,
        )
        report = BrokerReconciler(
            tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
            qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
        ).reconcile(
            strategy_state=strategy_state,
            brokers=brokers,
            tsm_symbol=config.live.binance_symbol,
            qff_symbol=reconciliation_qff_symbol(config, strategy_state),
            timestamp=timestamp,
        )
        run_id = store.record_reconciliation_report(report)
        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        close_brokers(brokers)
        store.close()

    print(
        "Broker reconciliation complete: "
        f"run_id={run_id}, status={report.status.value}, issues={len(report.issues)}"
    )
    for issue in report.issues:
        print(
            f"- {issue.status.value} {issue.issue_type} "
            f"{issue.broker.value} {issue.symbol or '-'} {issue.message}"
        )
    return 1 if report.status == ReconciliationStatus.ERROR else 0


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
            reporter.error(datetime.now().astimezone(), f"{type(exc).__name__}: {exc}")
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
        timestamp = datetime.now().astimezone().replace(microsecond=0)
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
                    timestamp=datetime.now().astimezone().replace(microsecond=0),
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
        LiveExecuteRunner(config, reporter=reporter).run(
            resume=args.resume,
            reset_store=args.reset_store,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except RuntimeError as exc:
        reporter.error(datetime.now().astimezone(), f"{type(exc).__name__}: {exc}")
        raise SystemExit(str(exc))
    finally:
        reporter.finish()
    return 0


def command_binance_exec_smoke(args: argparse.Namespace) -> int:
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
    adapter = FubonFutureExecutionAdapter(symbol, config.live.fubon_env_path)
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
            print("CRITICAL manual intervention required")
            return 1

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
        final_position = adapter.fetch_position_quantity()
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


def build_live_execution_gate_report(config: object, store: SQLiteStore):
    return evaluate_live_execution_gate(
        config,
        reconciliation_report=store.load_latest_reconciliation_report(),
        now=datetime.now().astimezone(),
        include_plan_checks=False,
    )


def command_live_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for live-paper")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_live_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    import ccxt

    observed_at = ensure_taipei(datetime.now().astimezone())
    session_status = live_session_status(
        observed_at,
        config.trading_calendar.closed_dates,
    )

    checks = [
        f"store_path={config.store_path}",
        f"polling_seconds={config.live.polling_seconds}",
        f"warmup_minutes={config.live.warmup_minutes}",
        f"qff_symbol={config.live.qff_symbol}",
        f"binance_symbol={config.live.binance_symbol}",
        f"bitopro_symbol={config.live.bitopro_symbol}",
        f"live_session={live_session_label(session_status)}",
        f"next_trading_start={session_status.next_open_at.isoformat()}",
        f"ccxt={ccxt.__version__}",
        f"live_order={config.safety.allow_live_order}",
    ]

    if live_marketdata_enabled():
        qff = FubonQffMarketData(config.live.fubon_env_path)
        try:
            qff_contract = resolve_qff_contract(config, qff)
            checks.append(f"qff_active_symbol={qff_contract.symbol}")
            checks.append(f"qff_active_expiry={qff_contract.expiry}")
            checks.append(f"qff_contract_policy={qff_contract.policy_state}")
            session_counts = getattr(qff, "last_candidate_session_counts", {})
            if session_counts:
                checks.append(
                    "qff_candidate_session_counts="
                    f"{json.dumps(session_counts, sort_keys=True)}"
                )
            session_summaries = getattr(qff, "last_candidate_session_summaries", {})
            if session_summaries:
                checks.append(
                    "qff_candidate_session_summaries="
                    f"{json.dumps(session_summaries, sort_keys=True)}"
                )
            if qff_contract.selection is not None:
                checks.append(
                    "qff_business_days_to_expiry="
                    f"{qff_contract.selection.business_days_to_expiry}"
                )
            try:
                qff.ensure_books_subscription(qff_contract.symbol)
                qff_quote = qff.fetch_quote(qff_contract.symbol)
                checks.append(
                    "qff_book="
                    f"price={qff_quote.price} bid={qff_quote.bid} ask={qff_quote.ask} "
                    f"bid_size={qff_quote.bid_size} ask_size={qff_quote.ask_size}"
                )
                checks.extend(
                    qff_book_diagnostic_lines(
                        qff_quote,
                        observed_at,
                        config.live.qff_book_stale_seconds,
                    )
                )
            except Exception as exc:
                checks.append(
                    "WARN qff_book_unavailable "
                    f"{type(exc).__name__}: {exc}"
                )
            binance_quote = CcxtTickerMarketData("binanceusdm").fetch_quote(
                config.live.binance_symbol
            )
            checks.append(
                "binance_book="
                f"price={binance_quote.price} bid={binance_quote.bid} "
                f"ask={binance_quote.ask} bid_size={binance_quote.bid_size} "
                f"ask_size={binance_quote.ask_size}"
            )
            bitopro_quote = CcxtTickerMarketData("bitopro").fetch_quote(
                config.live.bitopro_symbol
            )
            checks.append(
                "bitopro_book="
                f"price={bitopro_quote.price} bid={bitopro_quote.bid} "
                f"ask={bitopro_quote.ask} bid_size={bitopro_quote.bid_size} "
                f"ask_size={bitopro_quote.ask_size}"
            )
        finally:
            qff.close()

    print("Live doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def live_session_label(session_status: object) -> str:
    is_trading = bool(getattr(session_status, "is_trading"))
    is_close_only = bool(getattr(session_status, "is_close_only"))
    if not is_trading:
        return "closed"
    if is_close_only:
        return "close_only"
    return "open"


def qff_book_diagnostic_lines(
    qff_quote: object,
    observed_at: datetime,
    stale_seconds: float,
) -> list[str]:
    quote_timestamp = ensure_taipei(getattr(qff_quote, "timestamp"))
    age_sec = max((ensure_taipei(observed_at) - quote_timestamp).total_seconds(), 0.0)
    stale = age_sec > stale_seconds
    lines = [
        f"qff_book_timestamp={quote_timestamp.isoformat()}",
        f"qff_book_age_sec={age_sec:.3f}",
        f"qff_book_stale={str(stale).lower()}",
    ]
    if stale:
        lines.append(
            f"WARN stale_qff_book age_sec={age_sec:.3f} threshold={stale_seconds}"
        )
    return lines


def command_warmup_live(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = WarmupRunner(config).run(reset_store=args.reset_store)
    print(
        "Warmup complete: "
        f"bars_written={result.bars_written}, "
        f"qff_symbol={result.qff_symbol}, "
        f"start={result.start}, "
        f"end={result.end}"
    )
    return 0


def command_qff_warmup_check(args: argparse.Namespace) -> int:
    if not live_marketdata_enabled():
        raise SystemExit("Set LUX_LIVE_MARKETDATA=1 to run qff-warmup-check")
    config = load_config(args.config)
    result = QffWarmupCheckRunner(config).run(output_csv=args.output_csv)
    report = result.report
    print("QFF warmup check passed")
    print(f"- qff_symbol={result.qff_symbol}")
    print(f"- qff_expiry={result.qff_expiry}")
    print(f"- contract_policy_state={result.contract_policy_state}")
    print(f"- start={result.start}")
    print(f"- end={result.end}")
    print(f"- qff_fetch_start={result.qff_fetch_start}")
    print(f"- rows={len(report.frame)}")
    print(f"- source_rows={json.dumps(report.source_rows, sort_keys=True)}")
    print(
        f"- source_used_counts={json.dumps(report.source_used_counts, sort_keys=True)}"
    )
    print(f"- qff_close_filled_nulls={report.null_count}")
    print(f"- overlap_rows={report.overlap_rows}")
    print(f"- overlap_mismatch_count={report.mismatch_count}")
    print(f"- overlap_mismatch_max_abs_diff={report.max_abs_diff}")
    if result.output_csv is not None:
        print(f"- output_csv={result.output_csv}")
    return 0


def command_live_paper(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    reporter = (
        NullLiveReporter()
        if args.quiet_ui
        else LiveTerminalReporter(color=False if args.no_color else None)
    )
    try:
        result = LivePaperRunner(config, reporter=reporter).run(
            resume=args.resume,
            reset_store=args.reset_store,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except Exception as exc:
        reporter.error(datetime.now().astimezone(), f"{type(exc).__name__}: {exc}")
        raise
    reporter.finish()
    print(
        "Live-paper stopped: "
        f"iterations={result.iterations}, "
        f"bars_processed={result.bars_processed}, "
        f"skipped_minutes={result.skipped_minutes}, "
        f"qff_symbol={result.qff_symbol}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "replay":
        return command_replay(args)
    if args.command == "summary":
        return command_summary(args)
    if args.command == "doctor":
        return command_doctor(args)
    if args.command == "broker-doctor":
        return command_broker_doctor(args)
    if args.command == "reconcile-brokers":
        return command_reconcile_brokers(args)
    if args.command == "dry-run-doctor":
        return command_dry_run_doctor(args)
    if args.command == "execution-summary":
        return command_execution_summary(args)
    if args.command == "live-dry-run":
        return command_live_dry_run(args)
    if args.command == "simulate-execution":
        return command_simulate_execution(args)
    if args.command == "live-order-doctor":
        return command_live_order_doctor(args)
    if args.command == "live-execute":
        return command_live_execute(args)
    if args.command == "binance-exec-smoke":
        return command_binance_exec_smoke(args)
    if args.command == "fubon-exec-smoke":
        return command_fubon_exec_smoke(args)
    if args.command == "live-doctor":
        return command_live_doctor(args)
    if args.command == "warmup-live":
        return command_warmup_live(args)
    if args.command == "qff-warmup-check":
        return command_qff_warmup_check(args)
    if args.command == "live-paper":
        return command_live_paper(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
