from __future__ import annotations

import argparse
from pathlib import Path


def add_ui_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ui",
        choices=("dashboard", "compact"),
        default="compact",
        help="Live terminal UI style (default: compact; dashboard = rich panels)",
    )
    parser.add_argument(
        "--quiet-ui",
        action="store_true",
        help="Disable live terminal UI and print only the final summary",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Keep live terminal UI but disable colors",
    )


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True)


def add_pair_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pair",
        help="Strategy pair id (defaults to the sole configured pair)",
    )


def add_readonly_argument(
    parser: argparse.ArgumentParser,
    *,
    help_text: str,
) -> None:
    parser.add_argument(
        "--readonly",
        action="store_true",
        help=help_text,
    )


def add_live_loop_arguments(parser: argparse.ArgumentParser) -> None:
    add_config_argument(parser)
    add_pair_argument(parser)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reset-store", action="store_true")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them",
    )
    add_ui_arguments(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lux_trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="Run CSV replay into SQLite")
    add_config_argument(replay)
    add_pair_argument(replay)
    replay.add_argument("--max-bars", type=int)
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--reset-store", action="store_true")
    replay.set_defaults(route="replay")

    summary = subparsers.add_parser(
        "summary",
        help="Print SQLite replay summary (or execution summary with --execution)",
    )
    add_config_argument(summary)
    add_pair_argument(summary)
    summary.add_argument(
        "--execution",
        action="store_true",
        help="Print the execution plan/outcome summary instead of the replay summary",
    )
    summary.set_defaults(route="summary")

    live = subparsers.add_parser(
        "live",
        help="Run the live loop in an explicitly selected dry-run or execute mode",
    )
    live.add_argument(
        "--mode",
        choices=("dry-run", "execute"),
        required=True,
        help="Required live mode; execute retains all real-order safety gates",
    )
    add_live_loop_arguments(live)

    status = subparsers.add_parser(
        "status",
        help="Read configuration, strategy, reconciliation, margin, or broker status",
    )
    status_subparsers = status.add_subparsers(
        dest="status_command",
        required=True,
    )

    status_live = status_subparsers.add_parser(
        "live",
        help="Print persisted strategy state, position, and reconciliation (read-only)",
    )
    add_config_argument(status_live)
    add_pair_argument(status_live)
    status_live.set_defaults(route="status.live")

    status_broker = status_subparsers.add_parser(
        "broker",
        help="Run read-only broker checks and optional Fubon detail queries",
    )
    add_config_argument(status_broker)
    status_broker.add_argument(
        "--funds",
        action="store_true",
        help="Print the Fubon margin/equity snapshot (needs LUX_READONLY_BROKER=1)",
    )
    status_broker.add_argument(
        "--orders",
        metavar="SYMBOL",
        help="Print Fubon position/open-orders/order-records for SYMBOL "
        "(needs LUX_READONLY_BROKER=1)",
    )
    status_broker.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw broker rows for field-level audit",
    )
    status_broker.set_defaults(route="status.broker")

    status_doctor = status_subparsers.add_parser(
        "doctor",
        help="Check replay configuration, live market data, or live-order gates",
    )
    add_config_argument(status_doctor)
    status_doctor.add_argument(
        "--mode",
        choices=("replay", "live", "order"),
        default="replay",
        help="Which checks to run (live touches real market data only with "
        "LUX_LIVE_MARKETDATA=1; order prints the live execution gate report)",
    )
    status_doctor.set_defaults(route="status.doctor")

    status_reconcile = status_subparsers.add_parser(
        "reconcile",
        help="Run read-only broker/store reconciliation",
    )
    add_config_argument(status_reconcile)
    add_pair_argument(status_reconcile)
    add_readonly_argument(
        status_reconcile,
        help_text="Use real Fubon and Binance read-only brokers "
        "(requires LUX_READONLY_BROKER=1)",
    )
    status_reconcile.set_defaults(route="status.reconcile")

    status_margin = status_subparsers.add_parser(
        "margin",
        help="Read both accounts' margin ratios and print transfer guidance",
    )
    add_config_argument(status_margin)
    add_pair_argument(status_margin)
    status_margin.set_defaults(route="status.margin")

    recover = subparsers.add_parser(
        "recover",
        help="Run an explicit, guarded strategy recovery action",
    )
    recover_subparsers = recover.add_subparsers(
        dest="recover_command",
        required=True,
    )

    recover_clear_pause = recover_subparsers.add_parser(
        "clear-pause",
        help="Clear PAUSED to OPEN/FLAT after matched reconciliation",
    )
    add_config_argument(recover_clear_pause)
    add_pair_argument(recover_clear_pause)
    add_readonly_argument(
        recover_clear_pause,
        help_text="Use real Fubon and Binance read-only brokers "
        "(requires LUX_READONLY_BROKER=1)",
    )
    recover_clear_pause.set_defaults(route="recover.clear-pause")

    recover_manual_flat = recover_subparsers.add_parser(
        "manual-flat",
        help="Reconcile an externally manual-closed PAUSED position to flat",
    )
    add_config_argument(recover_manual_flat)
    add_pair_argument(recover_manual_flat)
    add_readonly_argument(
        recover_manual_flat,
        help_text="Verify both real brokers are flat "
        "(requires LUX_READONLY_BROKER=1)",
    )
    recover_manual_flat.add_argument(
        "--apply",
        action="store_true",
        help="Apply the audited exposure adjustment; default is dry-run",
    )
    recover_manual_flat.add_argument(
        "--reason",
        help="Required recovery reason when --apply is used",
    )
    recover_manual_flat.set_defaults(route="recover.manual-flat")

    warmup = subparsers.add_parser(
        "warmup",
        help="Seed live warmup bars (debug/acceptance tool)",
    )
    add_config_argument(warmup)
    add_pair_argument(warmup)
    warmup.add_argument("--reset-store", action="store_true")
    warmup.set_defaults(route="warmup")

    admin = subparsers.add_parser(
        "admin",
        help="Gated real-order administration; requires an explicit action",
    )
    admin_subparsers = admin.add_subparsers(
        dest="admin_command",
        required=True,
    )

    exec_smoke = admin_subparsers.add_parser(
        "exec-smoke",
        help="Run a tiny single-venue real entry/exit smoke behind every env gate",
    )
    add_config_argument(exec_smoke)
    exec_smoke.add_argument(
        "--venue",
        choices=("fubon", "binance"),
        required=True,
    )
    exec_smoke.add_argument(
        "--symbol",
        help="Fubon futures symbol (required for --venue fubon)",
    )
    exec_smoke.add_argument(
        "--lot",
        type=int,
        help="Fubon lot count (required for --venue fubon)",
    )
    exec_smoke.add_argument(
        "--quantity",
        type=float,
        help="Binance quantity (required for --venue binance)",
    )
    exec_smoke.add_argument("--confirm-symbol", required=True)
    exec_smoke.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows after the smoke",
    )
    exec_smoke.set_defaults(route="admin.exec-smoke")

    manual_close = admin_subparsers.add_parser(
        "manual-close",
        help="Emergency-close one stranded leg with a real market order behind gates",
    )
    add_config_argument(manual_close)
    manual_close.add_argument(
        "--venue",
        choices=("fubon", "binance"),
        required=True,
    )
    manual_close.add_argument("--symbol", required=True)
    manual_close.add_argument(
        "--side",
        choices=("buy", "sell"),
        required=True,
    )
    manual_close.add_argument(
        "--lot",
        type=int,
        help="Fubon lot count (required for --venue fubon)",
    )
    manual_close.add_argument(
        "--quantity",
        type=float,
        help="Binance quantity (required for --venue binance)",
    )
    manual_close.add_argument("--confirm-symbol", required=True)
    manual_close.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows",
    )
    manual_close.set_defaults(route="admin.manual-close")

    return parser
