"""Replay-strategy CLI command implementations.

These are the clean replay/summary/doctor bodies from the legacy
``cli/commands/replay.py`` without its live/execution/integration imports. They
depend only on the frozen mechanism (config, runner, store).
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy
import pandas

from lux_trader.config import load_config
from lux_trader.runner import SystemRunner
from lux_trader.store import SQLiteStore


def command_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("Refusing to run replay with allow_live_order=true")
    result = SystemRunner(config).replay(
        max_bars=args.max_bars,
        resume=args.resume,
        reset_store=args.reset_store,
    )
    print(
        "Replay complete: "
        f"rows_processed={result.rows_processed}, "
        f"start_row={result.start_row}, "
        f"end_row={result.end_row}, "
        f"finalized={result.finalized}"
    )
    return 0


def command_summary(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        if getattr(args, "execution", False):
            print(json.dumps(store.build_execution_summary(), indent=2))
        else:
            print(
                json.dumps(store.build_summary(config.strategy, config.fees), indent=2)
            )
    finally:
        store.close()
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    mode = getattr(args, "mode", "replay")
    if mode == "live":
        from .commands_live import run_live_doctor_checks

        checks = run_live_doctor_checks(config)
        print("Live doctor checks passed")
        for check in checks:
            print(f"- {check}")
        return 0
    if mode == "order":
        from .commands_execution import run_order_doctor_checks

        passed, lines = run_order_doctor_checks(config)
        print(f"Live execution gate status={'open' if passed else 'closed'}")
        for line in lines:
            print(f"- {line}")
        return 0
    checks: list[str] = []
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for replay")
    if not config.input_csv.exists():
        raise SystemExit(f"Input CSV does not exist: {config.input_csv}")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    checks.append(f"input_csv={config.input_csv}")
    checks.append(f"store_path={config.store_path}")
    checks.append(f"python={sys.version.split()[0]}")
    checks.append(f"pandas={pandas.__version__}")
    checks.append(f"numpy={numpy.__version__}")
    checks.append(f"live_order={config.safety.allow_live_order}")
    print("Doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0
