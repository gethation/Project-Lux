from __future__ import annotations

import argparse
from pathlib import Path

from lux_trader.execution.simulation import ExecutionSimulationScenario


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

    fubon_account_funds = subparsers.add_parser(
        "fubon-account-funds",
        help="Print Fubon futures account margin/equity using read-only API",
    )
    fubon_account_funds.add_argument("--config", type=Path, required=True)
    fubon_account_funds.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon margin rows for field-level audit",
    )

    fubon_order_records = subparsers.add_parser(
        "fubon-order-records",
        help="Print Fubon futures position, open orders, and order records read-only",
    )
    fubon_order_records.add_argument("--config", type=Path, required=True)
    fubon_order_records.add_argument("--symbol", required=True)
    fubon_order_records.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows",
    )

    fubon_manual_close = subparsers.add_parser(
        "fubon-manual-close",
        help="Emergency close a Fubon futures position with market IOC",
    )
    fubon_manual_close.add_argument("--config", type=Path, required=True)
    fubon_manual_close.add_argument("--symbol", required=True)
    fubon_manual_close.add_argument("--side", choices=("buy", "sell"), required=True)
    fubon_manual_close.add_argument("--lot", type=int, required=True)
    fubon_manual_close.add_argument("--confirm-symbol", required=True)
    fubon_manual_close.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows after manual close",
    )

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
    fubon_exec_smoke.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows after the smoke",
    )

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

