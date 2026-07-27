from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import lux_trader.cli.dispatch as dispatch
from lux_trader.cli.parser import build_parser


CONFIG = "config.toml"
PAIR_ID = "qff_tsm"

# Route resolution reads the [[pairs]] catalog, so dispatch-level tests need a
# config that actually exists, unlike the parse-only cases above.
REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_CONFIG = str(REPO_ROOT / "configs" / "replay.fixture.toml")
MULTIPAIR_CONFIG = str(REPO_ROOT / "tests" / "fixtures" / "config" / "multipair.toml")


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
            # live is the one command that drives several pairs, so its --pair is
            # repeatable and parses into a list of `id[:mode]` specs.
            "pair": [PAIR_ID],
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
            "pair": [PAIR_ID],
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


def leaf_parsers(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...] = (),
):
    """Walk the subparser tree, yielding (command path, parser) for each leaf."""
    nested = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    if not nested:
        yield prefix, parser
        return
    for action in nested:
        for name, sub in action.choices.items():
            yield from leaf_parsers(sub, prefix + (name,))


def test_every_config_taking_command_also_takes_pair() -> None:
    """--config without --pair is unusable against a multi-pair config.

    ``load_config`` refuses to guess when several pairs are enabled, so a command
    that accepts a config but offers no way to name a pair fails at load time --
    which is how ``status doctor``, ``status broker`` and both ``admin`` recovery
    commands broke once a second pair was configured. Asserted structurally so a
    newly added command cannot reintroduce it.
    """
    missing = [
        " ".join(path)
        for path, sub in leaf_parsers(build_parser())
        if "--config" in (options := {
            option
            for action in sub._actions
            for option in action.option_strings
        })
        and "--pair" not in options
    ]

    assert missing == []


def test_pair_id_is_resolved_from_config_instead_of_parser_choices() -> None:
    args = build_parser().parse_args(
        ["replay", "--config", CONFIG, "--pair", "configured_pair"]
    )

    assert args.pair == "configured_pair"


@pytest.mark.parametrize(
    "argv",
    (
        ["status"],
        ["recover"],
        ["admin"],
    ),
)
def test_nested_action_is_required(argv: list[str]) -> None:
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


def capture_route(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> list[argparse.Namespace]:
    calls: list[argparse.Namespace] = []

    def handler(args: argparse.Namespace) -> int:
        calls.append(args)
        return 17

    monkeypatch.setitem(dispatch.COMMAND_HANDLERS, route, handler)
    return calls


def test_live_dry_run_without_pair_runs_every_enabled_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_route(monkeypatch, "live.dry-run")

    assert dispatch.main(["live", "--mode", "dry-run", "--config", FIXTURE_CONFIG]) == 17

    assert len(calls) == 1
    assert calls[0].mode == "dry-run"
    # The list of specs is collapsed to the single resolved pair id the
    # single-pair command handlers still expect.
    assert calls[0].pair == "qff_tsm"


def test_live_execute_is_refused_without_pair(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§5.1: widening real-order exposure has to be visible on the command line."""
    calls = capture_route(monkeypatch, "live.execute")

    with pytest.raises(SystemExit):
        dispatch.main(["live", "--mode", "execute", "--config", FIXTURE_CONFIG])

    assert calls == []
    assert "without --pair" in capsys.readouterr().err


def test_live_requires_a_mode_from_somewhere(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        dispatch.main(["live", "--config", FIXTURE_CONFIG])

    assert "--mode is required" in capsys.readouterr().err


@pytest.mark.parametrize("mode", ("dry-run", "execute"))
def test_per_pair_mode_suffix_selects_the_route(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_route(monkeypatch, f"live.{mode}")

    assert (
        dispatch.main(
            ["live", "--config", FIXTURE_CONFIG, "--pair", f"qff_tsm:{mode}"]
        )
        == 17
    )

    assert len(calls) == 1
    assert calls[0].mode == mode
    assert calls[0].pair == "qff_tsm"


def test_pair_suffix_overrides_the_global_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_route(monkeypatch, "live.dry-run")

    assert (
        dispatch.main(
            [
                "live",
                "--mode",
                "execute",
                "--config",
                FIXTURE_CONFIG,
                "--pair",
                "qff_tsm:dry-run",
            ]
        )
        == 17
    )

    assert len(calls) == 1
    assert calls[0].mode == "dry-run"


def test_all_dry_run_multi_pair_selection_routes_with_pairs_multi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_route(monkeypatch, "live.dry-run")

    assert (
        dispatch.main(
            [
                "live",
                "--mode",
                "dry-run",
                "--config",
                MULTIPAIR_CONFIG,
            ]
        )
        == 17
    )

    assert len(calls) == 1
    assert calls[0].pairs_multi == ["qff_tsm", "ccf_umc"]
    assert calls[0].pair == "qff_tsm"


def test_execute_among_multiple_selections_is_still_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        dispatch.main(
            [
                "live",
                "--config",
                MULTIPAIR_CONFIG,
                "--pair",
                "qff_tsm:execute",
                "--pair",
                "ccf_umc:dry-run",
            ]
        )

    assert "execute among" in capsys.readouterr().err


def test_single_pair_selection_has_no_pairs_multi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_route(monkeypatch, "live.dry-run")

    assert (
        dispatch.main(
            ["live", "--config", FIXTURE_CONFIG, "--pair", "qff_tsm:dry-run"]
        )
        == 17
    )

    assert calls[0].pairs_multi is None
