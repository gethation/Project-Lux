from __future__ import annotations

from .commands import command_doctor, command_replay, command_summary
from .commands_execution import (
    command_broker_status,
    command_exec_smoke,
    command_live_execute,
    command_manual_close,
)
from .commands_live import (
    command_clear_pause,
    command_live_dry_run,
    command_live_status,
    command_margin_check,
    command_reconcile_brokers,
    command_warmup_live,
)
from .parser import build_parser


COMMAND_HANDLERS = {
    "replay": command_replay,
    "summary": command_summary,
    "doctor": command_doctor,
    "live-dry-run": command_live_dry_run,
    "live-status": command_live_status,
    "reconcile-brokers": command_reconcile_brokers,
    "clear-pause": command_clear_pause,
    "warmup-live": command_warmup_live,
    "margin-check": command_margin_check,
    "live-execute": command_live_execute,
    "exec-smoke": command_exec_smoke,
    "manual-close": command_manual_close,
    "broker-status": command_broker_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.error(f"Unknown command: {args.command}")
        return 2
    return handler(args)
