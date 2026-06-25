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


def live_marketdata_enabled() -> bool:
    return os.getenv(LIVE_MARKETDATA_ENV, LIVE_MARKETDATA_DEFAULT).strip() == "1"


def command_live_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for live-paper")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_live_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    import ccxt

    observed_at = ensure_taipei(cli_attr("datetime", datetime).now().astimezone())
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
            binance_quote = BinanceMarketData().fetch_quote(
                config.live.binance_symbol
            )
            checks.append(
                "binance_book="
                f"price={binance_quote.price} bid={binance_quote.bid} "
                f"ask={binance_quote.ask} bid_size={binance_quote.bid_size} "
                f"ask_size={binance_quote.ask_size}"
            )
            bitopro_quote = BitoProMarketData().fetch_quote(
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
        reporter.error(cli_attr("datetime", datetime).now().astimezone(), f"{type(exc).__name__}: {exc}")
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

