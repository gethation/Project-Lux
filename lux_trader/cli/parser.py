"""The CLI surface: seven top-level commands.

Consolidated from fourteen flat ones. The grouping is not cosmetic -- it puts
the dangerous verbs somewhere you have to go on purpose:

  replay / summary / warmup   local, no venue contact
  live --mode                 the trading loop, dry-run or execute
  status <sub>                diagnostics and read-only account queries
  recover <sub>               repairs persisted state after an incident
  admin <sub>                 SENDS REAL ORDERS behind env gates

Legacy name -> new name, for anyone with muscle memory or an old runbook:

  live-dry-run          -> live --mode dry-run
  live-execute          -> live --mode execute
  doctor                -> status doctor
  live-status           -> status live
  broker-status         -> status broker
  reconcile-brokers     -> status reconcile
  margin-check          -> status margin
  clear-pause           -> recover clear-pause
  recover-manual-flat   -> recover manual-flat
  warmup-live           -> warmup
  exec-smoke            -> admin exec-smoke
  manual-close          -> admin manual-close
"""

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


def add_readonly_argument(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument("--readonly", action="store_true", help=help_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lux_trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- replay ------------------------------------------------------------
    replay = subparsers.add_parser("replay", help="Run CSV replay into SQLite")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--max-bars", type=int)
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--reset-store", action="store_true")

    # -- summary -----------------------------------------------------------
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

    # -- warmup ------------------------------------------------------------
    warmup = subparsers.add_parser(
        "warmup",
        help="Seed live warmup bars (debug/acceptance tool)",
    )
    warmup.add_argument("--config", type=Path, required=True)
    warmup.add_argument("--reset-store", action="store_true")

    # -- live --------------------------------------------------------------
    live = subparsers.add_parser(
        "live",
        help="Run the live loop: --mode dry-run (simulated fills) or "
        "--mode execute (REAL two-leg orders, all safety gates must be open)",
    )
    live.add_argument("--config", type=Path, required=True)
    live.add_argument(
        "--mode",
        choices=("dry-run", "execute"),
        required=True,
        help="dry-run rehearses with a simulated execution adapter; "
        "execute sends real orders",
    )
    live.add_argument("--resume", action="store_true")
    live.add_argument("--reset-store", action="store_true")
    live.add_argument("--max-iterations", type=int)
    live.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them",
    )
    add_ui_arguments(live)

    # -- status ------------------------------------------------------------
    status = subparsers.add_parser(
        "status",
        help="Diagnostics and read-only queries",
    )
    status_subparsers = status.add_subparsers(dest="status_command", required=True)

    status_doctor = status_subparsers.add_parser(
        "doctor",
        help="Check configuration (replay by default; --mode live/order/ibkr "
        "for live market data / live-order gate / UMC entitlement checks)",
    )
    status_doctor.add_argument("--config", type=Path, required=True)
    status_doctor.add_argument(
        "--mode",
        choices=("replay", "live", "order", "ibkr"),
        default="replay",
        help="Which checks to run (live touches real market data only with "
        "LUX_LIVE_MARKETDATA=1; order prints the live execution gate report; "
        "ibkr probes IB Gateway for the UMC tier and bid/ask, and exits "
        "non-zero when the book is missing)",
    )

    status_live = status_subparsers.add_parser(
        "live",
        help="Print persisted strategy state, position, and latest reconciliation "
        "(read-only)",
    )
    status_live.add_argument("--config", type=Path, required=True)

    status_broker = status_subparsers.add_parser(
        "broker",
        help="Read-only broker checks: config/skeleton by default, account "
        "snapshots with LUX_READONLY_BROKER=1, --funds / --orders for Fubon "
        "details",
    )
    status_broker.add_argument("--config", type=Path, required=True)
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

    status_reconcile = status_subparsers.add_parser(
        "reconcile",
        help="Run broker/store reconciliation and record the report",
    )
    status_reconcile.add_argument("--config", type=Path, required=True)
    add_readonly_argument(
        status_reconcile,
        "Use real Fubon and IBKR read-only brokers (requires LUX_READONLY_BROKER=1)",
    )

    status_margin = status_subparsers.add_parser(
        "margin",
        help="Read both accounts' margin ratios and print transfer guidance "
        "(read-only; requires LUX_READONLY_BROKER=1)",
    )
    status_margin.add_argument("--config", type=Path, required=True)

    # -- recover -----------------------------------------------------------
    recover = subparsers.add_parser(
        "recover",
        help="Repair persisted strategy state after an incident",
    )
    recover_subparsers = recover.add_subparsers(dest="recover_command", required=True)

    recover_clear_pause = recover_subparsers.add_parser(
        "clear-pause",
        help="Clear a PAUSED strategy back to OPEN/FLAT after matched reconciliation",
    )
    recover_clear_pause.add_argument("--config", type=Path, required=True)
    add_readonly_argument(
        recover_clear_pause,
        "Use real Fubon and IBKR read-only brokers (requires LUX_READONLY_BROKER=1)",
    )

    recover_manual_flat = recover_subparsers.add_parser(
        "manual-flat",
        help="Reconcile an externally manual-closed PAUSED position to flat "
        "without inventing fill prices",
    )
    recover_manual_flat.add_argument("--config", type=Path, required=True)
    add_readonly_argument(
        recover_manual_flat,
        "Verify both real brokers are flat (requires LUX_READONLY_BROKER=1)",
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

    # -- admin -------------------------------------------------------------
    admin = subparsers.add_parser(
        "admin",
        help="Operations that SEND REAL ORDERS behind explicit env gates",
    )
    admin_subparsers = admin.add_subparsers(dest="admin_command", required=True)

    exec_smoke = admin_subparsers.add_parser(
        "exec-smoke",
        help="Run a tiny single-venue real entry/exit adapter smoke "
        "(SENDS REAL ORDERS behind env gates)",
    )
    exec_smoke.add_argument("--config", type=Path, required=True)
    # Only fubon until the IBKR execution adapter lands in Phase D.
    exec_smoke.add_argument("--venue", choices=("fubon",), required=True)
    exec_smoke.add_argument(
        "--symbol", help="Fubon futures symbol (required for --venue fubon)"
    )
    exec_smoke.add_argument(
        "--lot", type=int, help="Fubon lot count (required for --venue fubon)"
    )
    exec_smoke.add_argument("--confirm-symbol", required=True)
    exec_smoke.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows after the smoke",
    )

    manual_close = admin_subparsers.add_parser(
        "manual-close",
        help="Emergency-close a single stranded leg with a market order "
        "(SENDS A REAL ORDER behind env gates)",
    )
    manual_close.add_argument("--config", type=Path, required=True)
    manual_close.add_argument("--venue", choices=("fubon", "ibkr"), required=True)
    manual_close.add_argument("--symbol", required=True)
    manual_close.add_argument("--side", choices=("buy", "sell"), required=True)
    manual_close.add_argument(
        "--lot", type=int, help="Fubon lot count (required for --venue fubon)"
    )
    manual_close.add_argument(
        "--shares", type=int, help="UMC share count (required for --venue ibkr)"
    )
    manual_close.add_argument(
        "--allow-position-mismatch",
        action="store_true",
        help="IBKR only: proceed even though the requested close does not match "
        "the broker's reported position. Refused by default, because a close "
        "larger than the position opens the opposite one",
    )
    manual_close.add_argument("--confirm-symbol", required=True)
    manual_close.add_argument(
        "--raw-json",
        action="store_true",
        help="Print raw Fubon order result rows",
    )

    return parser
