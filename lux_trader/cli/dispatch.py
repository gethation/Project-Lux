from __future__ import annotations

from .parser import build_parser
from .commands import (
    command_binance_exec_smoke,
    command_broker_doctor,
    command_doctor,
    command_dry_run_doctor,
    command_execution_summary,
    command_fubon_account_funds,
    command_fubon_exec_smoke,
    command_fubon_manual_close,
    command_fubon_order_records,
    command_live_doctor,
    command_live_dry_run,
    command_live_execute,
    command_live_order_doctor,
    command_live_paper,
    command_qff_warmup_check,
    command_reconcile_brokers,
    command_replay,
    command_simulate_execution,
    command_summary,
    command_warmup_live,
)


COMMAND_HANDLERS = {
    "replay": command_replay,
    "summary": command_summary,
    "doctor": command_doctor,
    "broker-doctor": command_broker_doctor,
    "fubon-account-funds": command_fubon_account_funds,
    "fubon-order-records": command_fubon_order_records,
    "fubon-manual-close": command_fubon_manual_close,
    "reconcile-brokers": command_reconcile_brokers,
    "dry-run-doctor": command_dry_run_doctor,
    "execution-summary": command_execution_summary,
    "live-dry-run": command_live_dry_run,
    "simulate-execution": command_simulate_execution,
    "live-order-doctor": command_live_order_doctor,
    "live-execute": command_live_execute,
    "binance-exec-smoke": command_binance_exec_smoke,
    "fubon-exec-smoke": command_fubon_exec_smoke,
    "live-doctor": command_live_doctor,
    "warmup-live": command_warmup_live,
    "qff-warmup-check": command_qff_warmup_check,
    "live-paper": command_live_paper,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"Unknown command: {args.command}")
        return 2
    return handler(args)
