from __future__ import annotations

import argparse
import sys

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


# 128 + SIGINT, the shell convention, so the PowerShell launcher's `if ($?)` can
# still tell a deliberate stop from a failure.
SIGINT_EXIT_CODE = 130


def run(argv: list[str] | None = None) -> int:
    """`main`, with Ctrl+C reported as a stop rather than a crash.

    Ctrl+C is how a live session is meant to end, so it should not print a
    traceback. Nothing about the shutdown depends on this: every command does
    its cleanup in a `finally` -- close brokers, release the lease, finish the
    reporter -- and those run while KeyboardInterrupt propagates through them.
    Nor was an interrupt ever written to the crash log: record_crash is bound to
    `except Exception`, and KeyboardInterrupt is a BaseException.

    The workers are handled separately. Windows delivers CTRL_C_EVENT to every
    process on the console, so each spawned child used to raise and print its
    own traceback; they now ignore SIGINT and are shut down by the parent.
    See integrations/subprocess_transport.ignore_parent_interrupt.
    """
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\nInterrupted - shutting down.", file=sys.stderr)
        return SIGINT_EXIT_CODE
