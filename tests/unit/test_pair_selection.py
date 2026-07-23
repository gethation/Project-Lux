"""Selection rules from §5.1 of docs/MULTIPAIR_PLAN.md.

The two that carry real risk: a disabled pair must be unreachable however it is
named, and ``execute`` must never be reachable without spelling the pair out on
the command line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lux_trader.cli.pair_selection import (
    PairSelection,
    PairSelectionError,
    has_execute_selection,
    parse_pair_spec,
    resolve_pair_selections,
)
from lux_trader.config import load_pair_catalog


REPO_ROOT = Path(__file__).resolve().parents[2]
MULTIPAIR_CONFIG = REPO_ROOT / "tests" / "fixtures" / "config" / "multipair.toml"


@pytest.fixture(scope="module")
def catalog():
    return load_pair_catalog(MULTIPAIR_CONFIG)


@pytest.mark.parametrize(
    ("spec", "expected"),
    (
        ("qff_tsm", ("qff_tsm", None)),
        ("qff_tsm:execute", ("qff_tsm", "execute")),
        ("qff_tsm:dry-run", ("qff_tsm", "dry-run")),
        ("  qff_tsm : EXECUTE  ", ("qff_tsm", "execute")),
    ),
)
def test_parse_pair_spec(spec: str, expected: tuple[str, str | None]) -> None:
    assert parse_pair_spec(spec) == expected


@pytest.mark.parametrize("spec", ("", "   ", ":execute", "qff_tsm:live", "qff_tsm:"))
def test_parse_pair_spec_rejects_malformed_input(spec: str) -> None:
    with pytest.raises(PairSelectionError):
        parse_pair_spec(spec)


def test_dry_run_without_pair_expands_to_every_enabled_pair(catalog) -> None:
    selections = resolve_pair_selections(
        catalog,
        requested=None,
        default_mode="dry-run",
    )

    assert selections == (
        PairSelection("qff_tsm", "dry-run"),
        PairSelection("ccf_umc", "dry-run"),
    )
    # The disabled pair is absent without having to be excluded by name.
    assert all(selection.pair_id != "retired_pair" for selection in selections)


def test_execute_without_pair_is_refused(catalog) -> None:
    with pytest.raises(PairSelectionError, match="without --pair"):
        resolve_pair_selections(catalog, requested=None, default_mode="execute")


def test_missing_mode_without_default_is_refused(catalog) -> None:
    with pytest.raises(PairSelectionError, match="carries no mode"):
        resolve_pair_selections(
            catalog,
            requested=["qff_tsm"],
            default_mode=None,
        )


def test_mixed_modes_are_expressible(catalog) -> None:
    selections = resolve_pair_selections(
        catalog,
        requested=["qff_tsm:execute", "ccf_umc:dry-run"],
        default_mode=None,
    )

    assert selections == (
        PairSelection("qff_tsm", "execute"),
        PairSelection("ccf_umc", "dry-run"),
    )
    assert has_execute_selection(selections)


def test_suffix_beats_the_default_mode(catalog) -> None:
    selections = resolve_pair_selections(
        catalog,
        requested=["qff_tsm:dry-run", "ccf_umc"],
        default_mode="execute",
    )

    assert selections == (
        PairSelection("qff_tsm", "dry-run"),
        PairSelection("ccf_umc", "execute"),
    )


def test_disabled_pair_cannot_be_selected_even_by_name(catalog) -> None:
    with pytest.raises(PairSelectionError, match="disabled"):
        resolve_pair_selections(
            catalog,
            requested=["retired_pair:dry-run"],
            default_mode=None,
        )


def test_unknown_pair_lists_what_is_available(catalog) -> None:
    with pytest.raises(PairSelectionError, match="not configured"):
        resolve_pair_selections(
            catalog,
            requested=["nope"],
            default_mode="dry-run",
        )


def test_repeating_a_pair_is_refused(catalog) -> None:
    with pytest.raises(PairSelectionError, match="more than once"):
        resolve_pair_selections(
            catalog,
            requested=["qff_tsm:execute", "qff_tsm:dry-run"],
            default_mode=None,
        )


def test_selection_order_follows_the_command_line(catalog) -> None:
    selections = resolve_pair_selections(
        catalog,
        requested=["ccf_umc:dry-run", "qff_tsm:execute"],
        default_mode=None,
    )

    assert [selection.pair_id for selection in selections] == ["ccf_umc", "qff_tsm"]


def test_catalog_carries_enabled_and_weekend_policy(catalog) -> None:
    by_id = {pair.id: pair for pair in catalog}

    assert by_id["qff_tsm"].enabled is True
    # Unset means 'flat', which is what the frozen replay baseline was measured under.
    assert by_id["qff_tsm"].weekend_policy == "flat"
    assert by_id["ccf_umc"].weekend_policy == "none"
    assert by_id["retired_pair"].enabled is False
