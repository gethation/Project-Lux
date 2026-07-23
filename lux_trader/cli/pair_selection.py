"""Resolve which pairs a live run drives, and in which mode.

A single process has to be able to run one pair for real while another only
simulates -- Fubon allows one SDK session per account, so both pairs share a
process whether or not they share a risk appetite. The mode therefore belongs to
the pair, not to the run: ``--pair qff_tsm:execute --pair ccf_umc:dry-run``.

``--mode`` survives as the default for pairs that omit their own suffix, which
keeps the single-pair form (``live --mode execute --pair qff_tsm``) working
unchanged. Two rules from the plan's §5.1 are enforced here:

* a disabled pair cannot be selected at all, however it is named;
* ``execute`` is never reachable without naming the pair on the command line,
  so adding a pair to a config can never silently widen live exposure.
"""

from __future__ import annotations

from dataclasses import dataclass

from lux_trader.config import PairConfig


MODE_DRY_RUN = "dry-run"
MODE_EXECUTE = "execute"
LIVE_MODES = (MODE_DRY_RUN, MODE_EXECUTE)


@dataclass(frozen=True)
class PairSelection:
    pair_id: str
    mode: str


class PairSelectionError(RuntimeError):
    """Raised when the requested pair/mode combination is not allowed."""


def parse_pair_spec(spec: str) -> tuple[str, str | None]:
    """Split ``id`` or ``id:mode`` into its parts.

    The mode is returned as None when the spec carries no suffix, leaving the
    caller to fall back to ``--mode``.
    """
    text = spec.strip()
    if not text:
        raise PairSelectionError("--pair may not be empty")
    pair_id, separator, mode_text = text.partition(":")
    pair_id = pair_id.strip()
    if not pair_id:
        raise PairSelectionError(f"--pair {spec!r} is missing a pair id")
    if not separator:
        return pair_id, None
    mode = mode_text.strip().lower()
    if mode not in LIVE_MODES:
        allowed = ", ".join(LIVE_MODES)
        raise PairSelectionError(
            f"--pair {spec!r} has an unknown mode {mode_text.strip()!r}; "
            f"expected one of: {allowed}"
        )
    return pair_id, mode


def resolve_pair_selections(
    pairs: tuple[PairConfig, ...],
    *,
    requested: list[str] | None,
    default_mode: str | None,
) -> tuple[PairSelection, ...]:
    """Turn the parsed CLI arguments into an ordered, validated selection."""
    by_id = {pair.id: pair for pair in pairs}

    if not requested:
        return _resolve_implicit(pairs, default_mode=default_mode)

    selections: list[PairSelection] = []
    seen: set[str] = set()
    for spec in requested:
        pair_id, mode = parse_pair_spec(spec)
        pair = by_id.get(pair_id)
        if pair is None:
            available = ", ".join(sorted(by_id)) or "(none)"
            raise PairSelectionError(
                f"Pair {pair_id!r} is not configured; available pairs: {available}"
            )
        if not pair.enabled:
            raise PairSelectionError(
                f"Pair {pair_id!r} is disabled in config (enabled = false); "
                "re-enable it there before selecting it"
            )
        if pair_id in seen:
            raise PairSelectionError(f"Pair {pair_id!r} was selected more than once")
        if mode is None:
            if default_mode is None:
                raise PairSelectionError(
                    f"--pair {spec!r} carries no mode and --mode was not given; "
                    f"use --pair {pair_id}:<{'|'.join(LIVE_MODES)}> or pass --mode"
                )
            mode = default_mode
        seen.add(pair_id)
        selections.append(PairSelection(pair_id=pair_id, mode=mode))
    return tuple(selections)


def _resolve_implicit(
    pairs: tuple[PairConfig, ...],
    *,
    default_mode: str | None,
) -> tuple[PairSelection, ...]:
    if default_mode is None:
        raise PairSelectionError("--mode is required when no --pair is given")
    if default_mode == MODE_EXECUTE:
        # §5.1: widening real-order exposure must be visible on the command line.
        raise PairSelectionError(
            "Refusing --mode execute without --pair; name every pair to trade, "
            "for example: --pair <id>:execute"
        )
    enabled = [pair for pair in pairs if pair.enabled]
    if not enabled:
        raise PairSelectionError("No pair is enabled in this config")
    return tuple(
        PairSelection(pair_id=pair.id, mode=default_mode) for pair in enabled
    )


def has_execute_selection(selections: tuple[PairSelection, ...]) -> bool:
    return any(selection.mode == MODE_EXECUTE for selection in selections)
