from __future__ import annotations

import argparse

from .commands import command_doctor, command_replay, command_summary
from .commands_execution import (
    command_broker_status,
    command_exec_smoke,
    command_live_execute,
    command_manual_close,
)
from .commands_recovery import command_recover_manual_flat
from .commands_live import (
    command_clear_pause,
    command_live_dry_run,
    command_live_status,
    command_margin_check,
    command_reconcile_brokers,
    command_warmup_live,
)
from .parser import build_parser


def command_live(args: argparse.Namespace) -> int:
    if args.mode == "execute":
        return command_live_execute(args)
    return command_live_dry_run(args)


# (command, subcommand-or-None) -> handler. The subcommand attribute is named
# after its group, so one lookup covers both flat and nested commands.
COMMAND_HANDLERS = {
    ("replay", None): command_replay,
    ("summary", None): command_summary,
    ("warmup", None): command_warmup_live,
    ("live", None): command_live,
    ("status", "doctor"): command_doctor,
    ("status", "live"): command_live_status,
    ("status", "broker"): command_broker_status,
    ("status", "reconcile"): command_reconcile_brokers,
    ("status", "margin"): command_margin_check,
    ("recover", "clear-pause"): command_clear_pause,
    ("recover", "manual-flat"): command_recover_manual_flat,
    ("admin", "exec-smoke"): command_exec_smoke,
    ("admin", "manual-close"): command_manual_close,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    subcommand = getattr(args, f"{args.command}_command", None)
    handler = COMMAND_HANDLERS.get((args.command, subcommand))
    if handler is None:
        parser.error(f"Unknown command: {args.command} {subcommand or ''}".strip())
        return 2
    return handler(args)
