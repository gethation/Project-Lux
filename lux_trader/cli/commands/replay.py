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

