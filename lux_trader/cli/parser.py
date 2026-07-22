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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lux_trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="Run CSV replay into SQLite")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--max-bars", type=int)
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--reset-store", action="store_true")

    summary = subparsers.add_parser(
        "summary",
        help="Print SQLite replay summary (or execution summary with --execution)",
    )
    summary.add_argument("--config", type=Path, required=True)
    summary.add_argument(
        "--execution",
        action="store_true",
        help="Print the execution plan/outcome summary instead of the replay summary",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="Check configuration (replay by default; --mode live/order for "
        "live market data / live-order gate checks)",
    )
    doctor.add_argument("--config", type=Path, required=True)
    doctor.add_argument(
        "--mode",
        choices=("replay", "live", "order"),
        default="replay",
        help="Which checks to run (live touches real market data only with "
        "LUX_LIVE_MARKETDATA=1; order prints the live execution gate report)",
    )

    live_dry_run = subparsers.add_parser(
        "live-dry-run",
        help="Run the full live rehearsal with a simulated execution adapter",
    )
    live_dry_run.add_argument("--config", type=Path, required=True)
    live_dry_run.add_argument("--resume", action="store_true")
    live_dry_run.add_argument("--reset-store", action="store_true")
    live_dry_run.add_argument("--max-iterations", type=int)
    live_dry_run.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them",
    )
    add_ui_arguments(live_dry_run)

    live_status = subparsers.add_parser(
        "live-status",
        help="Print persisted strategy state, position, and latest reconciliation "
        "(read-only)",
    )
    live_status.add_argument("--config", type=Path, required=True)

    reconcile_brokers = subparsers.add_parser(
        "reconcile-brokers",
        help="Run read-only broker/store reconciliation",
    )
    reconcile_brokers.add_argument("--config", type=Path, required=True)
    reconcile_brokers.add_argument(
        "--readonly",
        action="store_true",
        help="Use real Fubon and Binance read-only brokers "
        "(requires LUX_READONLY_BROKER=1)",
    )

    clear_pause = subparsers.add_parser(
        "clear-pause",
        help="Clear a PAUSED strategy back to OPEN/FLAT after matched reconciliation",
    )
    clear_pause.add_argument("--config", type=Path, required=True)
    clear_pause.add_argument(
        "--readonly",
        action="store_true",
        help="Use real Fubon and Binance read-only brokers "
        "(requires LUX_READONLY_BROKER=1)",
    )

    recover_manual_flat = subparsers.add_parser(
        "recover-manual-flat",
        help="Reconcile an externally manual-closed PAUSED position to flat "
        "without inventing fill prices",
    )
    recover_manual_flat.add_argument("--config", type=Path, required=True)
    recover_manual_flat.add_argument(
        "--readonly",
        action="store_true",
        help="Verify both real brokers are flat (requires LUX_READONLY_BROKER=1)",
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

    warmup_live = subparsers.add_parser(
        "warmup-live",
        help="Seed live warmup bars (debug/acceptance tool)",
    )
    warmup_live.add_argument("--config", type=Path, required=True)
    warmup_live.add_argument("--reset-store", action="store_true")

    margin_check = subparsers.add_parser(
        "margin-check",
        help="Read both accounts' margin ratios and print transfer guidance "
        "(read-only; requires LUX_READONLY_BROKER=1)",
    )
    margin_check.add_argument("--config", type=Path, required=True)

    live_execute = subparsers.add_parser(
        "live-execute",
        help="Run live execution with real two-leg orders (all safety gates "
        "must be open)",
    )
    live_execute.add_argument("--config", type=Path, required=True)
    live_execute.add_argument("--resume", action="store_true")
    live_execute.add_argument("--reset-store", action="store_true")
    live_execute.add_argument("--max-iterations", type=int)
    live_execute.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them",
    )
    add_ui_arguments(live_execute)

    exec_smoke = subparsers.add_parser(
        "exec-smoke",
        help="Run a tiny single-venue real entry/exit adapter smoke "
        "(SENDS REAL ORDERS behind env gates)",
    )
    exec_smoke.add_argument("--config", type=Path, required=True)
    exec_smoke.add_argument(
        "--venue", choices=("fubon", "binance"), required=True
    )
    exec_smoke.add_argument(
        "--symbol", help="Fubon futures symbol (required for --venue fubon)"
    )
    exec_smoke.add_argument(
        "--lot", type=int, help="Fubon lot count (required for --venue fubon)"
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

    manual_close = subparsers.add_parser(
        "manual-close",
        help="Emergency-close a single stranded leg with a market order "
        "(SENDS A REAL ORDER behind env gates)",
    )
    manual_close.add_argument("--config", type=Path, required=True)
    manual_close.add_argument(
        "--venue", choices=("fubon", "binance"), required=True
    )
    manual_close.add_argument("--symbol", required=True)
    manual_close.add_argument("--side", choices=("buy", "sell"), required=True)
    manual_close.add_argument(
        "--lot", type=int, help="Fubon lot count (required for --venue fubon)"
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

    broker_status = subparsers.add_parser(
        "broker-status",
        help="Read-only broker checks: config/skeleton by default, account "
        "snapshots with LUX_READONLY_BROKER=1, --funds / --orders for Fubon "
        "details",
    )
    broker_status.add_argument("--config", type=Path, required=True)
    broker_status.add_argument(
        "--funds",
        action="store_true",
        help="Print the Fubon margin/equity snapshot (needs LUX_READONLY_BROKER=1)",
    )
    broker_status.add_argument(
        "--orders",
        metavar="SYMBOL",
        help="Print Fubon position/open-orders/order-records for SYMBOL "
        "(needs LUX_READONLY_BROKER=1)",
    )
    broker_status.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw broker rows for field-level audit",
    )

    return parser
