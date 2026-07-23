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
from .commands_recovery import command_recover_manual_flat
from .parser import build_parser


COMMAND_HANDLERS = {
    "replay": command_replay,
    "summary": command_summary,
    "live.dry-run": command_live_dry_run,
    "live.execute": command_live_execute,
    "status.live": command_live_status,
    "status.broker": command_broker_status,
    "status.doctor": command_doctor,
    "status.reconcile": command_reconcile_brokers,
    "status.margin": command_margin_check,
    "recover.clear-pause": command_clear_pause,
    "recover.manual-flat": command_recover_manual_flat,
    "warmup": command_warmup_live,
    "admin.exec-smoke": command_exec_smoke,
    "admin.manual-close": command_manual_close,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    route = (
        f"live.{args.mode}"
        if args.command == "live"
        else getattr(args, "route", None)
    )
    handler = COMMAND_HANDLERS.get(route)
    if handler is None:
        parser.error(f"Unknown command route: {route}")
        return 2
    return handler(args)
