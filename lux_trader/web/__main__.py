"""``python -m lux_trader.web --config <cfg>``

Deliberately not a subcommand of the main CLI: this is an observer, and keeping
it out of ``lux_trader/cli`` means no file on the live-execute import path had
to change to add it.
"""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

from ..config import load_config
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m lux_trader.web",
        description="Read-only spread chart for a live or replay store.",
    )
    parser.add_argument("--config", required=True, type=Path, help="TOML config path")
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Override paths.store_path (e.g. point a live config at a replay store)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. 0.0.0.0 exposes it beyond this machine and turns on"
        " token auth (see --token).",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--token",
        default=os.getenv("LUX_WEB_TOKEN") or None,
        help="Shared secret required on every request. Generated automatically"
        " for a non-loopback bind if not given.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.store is not None:
        config = replace(config, store_path=args.store.expanduser().resolve())
    if not config.store_path.exists():
        # Not fatal: started alongside `live --reset-store`, the viewer comes up
        # before the store exists. The page reports the gap and starts working
        # on its own once the engine writes the first bar.
        print(f"note: store does not exist yet: {config.store_path}")
    serve(config, host=args.host, port=args.port, token=args.token)


if __name__ == "__main__":
    main()
