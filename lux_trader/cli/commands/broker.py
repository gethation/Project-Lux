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


from lux_trader.cli.commands.execution import (
    print_fubon_smoke_order_records,
    print_fubon_smoke_position,
)

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


def command_fubon_account_funds(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to query Fubon account funds")

    broker = cli_attr("FubonReadOnlyBroker", FubonReadOnlyBroker)(config.live.fubon_env_path)
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


def command_fubon_order_records(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if not readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to query Fubon order records")
    symbol = str(args.symbol).strip()
    if not symbol:
        raise SystemExit("--symbol is required")

    adapter = cli_attr("FubonFutureExecutionAdapter", FubonFutureExecutionAdapter)(
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

