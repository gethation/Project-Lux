from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import lux_trader.cli.dispatch as dispatch
from lux_trader.cli.parser import PAIR_ID, build_parser


CONFIG = "config.toml"


CLI_CASES = (
    (
        [
            "replay",
            "--config",
            CONFIG,
            "--max-bars",
            "3",
            "--resume",
            "--reset-store",
            "--pair",
            PAIR_ID,
        ],
        "replay",
        {
            "max_bars": 3,
            "resume": True,
            "reset_store": True,
            "pair": PAIR_ID,
        },
    ),
    (
        ["summary", "--config", CONFIG, "--execution", "--pair", PAIR_ID],
        "summary",
        {"execution": True, "pair": PAIR_ID},
    ),
    (
        [
            "live",
            "--mode",
            "dry-run",
            "--config",
            CONFIG,
            "--resume",
            "--reset-store",
            "--max-iterations",
            "4",
            "--skip-warmup",
            "--ui",
            "dashboard",
            "--quiet-ui",
            "--no-color",
            "--pair",
            PAIR_ID,
        ],
        "live.dry-run",
        {
            "mode": "dry-run",
            "resume": True,
            "reset_store": True,
            "max_iterations": 4,
            "skip_warmup": True,
            "ui": "dashboard",
            "quiet_ui": True,
            "no_color": True,
            "pair": PAIR_ID,
        },
    ),
    (
        [
            "live",
            "--mode",
            "execute",
            "--config",
            CONFIG,
            "--resume",
            "--reset-store",
            "--max-iterations",
            "5",
            "--skip-warmup",
            "--ui",
            "compact",
            "--quiet-ui",
            "--no-color",
            "--pair",
            PAIR_ID,
        ],
        "live.execute",
        {
            "mode": "execute",
            "resume": True,
            "reset_store": True,
            "max_iterations": 5,
            "skip_warmup": True,
            "ui": "compact",
            "quiet_ui": True,
            "no_color": True,
            "pair": PAIR_ID,
        },
    ),
    (
        ["status", "live", "--config", CONFIG, "--pair", PAIR_ID],
        "status.live",
        {"pair": PAIR_ID},
    ),
    (
        [
            "status",
            "broker",
            "--config",
            CONFIG,
            "--funds",
            "--orders",
            "QFFG6",
            "--raw-json",
        ],
        "status.broker",
        {"funds": True, "orders": "QFFG6", "raw_json": True},
    ),
    (
        ["status", "doctor", "--config", CONFIG, "--mode", "order"],
        "status.doctor",
        {"mode": "order"},
    ),
    (
        [
            "status",
            "reconcile",
            "--config",
            CONFIG,
            "--readonly",
            "--pair",
            PAIR_ID,
        ],
        "status.reconcile",
        {"readonly": True, "pair": PAIR_ID},
    ),
    (
        ["status", "margin", "--config", CONFIG, "--pair", PAIR_ID],
        "status.margin",
        {"pair": PAIR_ID},
    ),
    (
        [
            "recover",
            "clear-pause",
            "--config",
            CONFIG,
            "--readonly",
            "--pair",
            PAIR_ID,
        ],
        "recover.clear-pause",
        {"readonly": True, "pair": PAIR_ID},
    ),
    (
        [
            "recover",
            "manual-flat",
            "--config",
            CONFIG,
            "--readonly",
            "--apply",
            "--reason",
            "operator_verified",
            "--pair",
            PAIR_ID,
        ],
        "recover.manual-flat",
        {
            "readonly": True,
            "apply": True,
            "reason": "operator_verified",
            "pair": PAIR_ID,
        },
    ),
    (
        ["warmup", "--config", CONFIG, "--reset-store", "--pair", PAIR_ID],
        "warmup",
        {"reset_store": True, "pair": PAIR_ID},
    ),
    (
        [
            "admin",
            "exec-smoke",
            "--config",
            CONFIG,
            "--venue",
            "fubon",
            "--symbol",
            "QFFG6",
            "--lot",
            "1",
            "--quantity",
            "0.01",
            "--confirm-symbol",
            "QFFG6",
            "--raw-json",
        ],
        "admin.exec-smoke",
        {
            "venue": "fubon",
            "symbol": "QFFG6",
            "lot": 1,
            "quantity": 0.01,
            "confirm_symbol": "QFFG6",
            "raw_json": True,
        },
    ),
    (
        [
            "admin",
            "manual-close",
            "--config",
            CONFIG,
            "--venue",
            "binance",
            "--symbol",
            "TSM/USDT:USDT",
            "--side",
            "buy",
            "--lot",
            "1",
            "--quantity",
            "0.02",
            "--confirm-symbol",
            "TSM/USDT:USDT",
            "--raw-json",
        ],
        "admin.manual-close",
        {
            "venue": "binance",
            "symbol": "TSM/USDT:USDT",
            "side": "buy",
            "lot": 1,
            "quantity": 0.02,
            "confirm_symbol": "TSM/USDT:USDT",
            "raw_json": True,
        },
    ),
)


def command_route(args: argparse.Namespace) -> str:
    if args.command == "live":
        return f"live.{args.mode}"
    return args.route


@pytest.mark.parametrize(("argv", "expected_route", "expected_values"), CLI_CASES)
def test_every_legacy_flag_is_reachable_on_consolidated_route(
    argv: list[str],
    expected_route: str,
    expected_values: dict[str, object],
) -> None:
    args = build_parser().parse_args(argv)

    assert args.config == Path(CONFIG)
    assert command_route(args) == expected_route
    for name, value in expected_values.items():
        assert getattr(args, name) == value


def test_top_level_surface_is_exactly_seven_commands() -> None:
    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert tuple(subparsers_action.choices) == (
        "replay",
        "summary",
        "live",
        "status",
        "recover",
        "warmup",
        "admin",
    )


@pytest.mark.parametrize(
    "argv",
    (
        ["live", "--config", CONFIG],
        ["status"],
        ["recover"],
        ["admin"],
    ),
)
def test_explicit_live_mode_and_nested_action_are_required(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.mark.parametrize(
    "legacy_name",
    (
        "doctor",
        "live-dry-run",
        "live-status",
        "reconcile-brokers",
        "clear-pause",
        "recover-manual-flat",
        "warmup-live",
        "margin-check",
        "live-execute",
        "exec-smoke",
        "manual-close",
        "broker-status",
    ),
)
def test_retired_top_level_names_are_rejected(legacy_name: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([legacy_name])


@pytest.mark.parametrize(
    ("mode", "expected_route"),
    (("dry-run", "live.dry-run"), ("execute", "live.execute")),
)
def test_live_dispatch_requires_and_uses_explicit_mode(
    mode: str,
    expected_route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[argparse.Namespace] = []

    def handler(args: argparse.Namespace) -> int:
        calls.append(args)
        return 17

    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, expected_route, handler)

    assert (
        dispatch.main(["live", "--mode", mode, "--config", CONFIG])
        == 17
    )
    assert len(calls) == 1
    assert calls[0].mode == mode
