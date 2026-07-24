from __future__ import annotations

import argparse

from lux_trader.config import load_pair_catalog

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
from .pair_selection import PairSelectionError, resolve_pair_selections
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


def resolve_live_route(args: argparse.Namespace) -> str:
    """Collapse ``--pair``/``--mode`` into one route, rewriting args in place.

    ``args.pair`` arrives as a list of ``id[:mode]`` specs and leaves as the plain
    pair id the single-pair commands already expect, with ``args.mode`` set to
    that pair's resolved mode.
    """
    selections = resolve_pair_selections(
        load_pair_catalog(args.config),
        requested=args.pair,
        default_mode=args.mode,
    )
    if len(selections) > 1:
        if all(s.mode == "dry-run" for s in selections):
            # All-dry-run runs many pairs in one process. The primary pair id
            # stays on args.pair for the code paths that want one; the full
            # list rides on args.pairs_multi for the multi-pair command path.
            args.pair = selections[0].pair_id
            args.pairs_multi = [s.pair_id for s in selections]
            args.mode = "dry-run"
            return "live.dry-run"
        chosen = ", ".join(f"{s.pair_id}:{s.mode}" for s in selections)
        raise PairSelectionError(
            f"Selected {len(selections)} pairs ({chosen}) with execute among "
            "them. Mixed execute+dry-run in one process needs the "
            "allow_live_order gate rework and is its own upcoming slice; "
            "for now execute runs one pair per process."
        )
    selection = selections[0]
    args.pair = selection.pair_id
    args.pairs_multi = None
    args.mode = selection.mode
    return f"live.{selection.mode}"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "live":
        try:
            route = resolve_live_route(args)
        except PairSelectionError as exc:
            parser.error(str(exc))
            return 2
    else:
        route = getattr(args, "route", None)
    handler = COMMAND_HANDLERS.get(route)
    if handler is None:
        parser.error(f"Unknown command route: {route}")
        return 2
    return handler(args)
