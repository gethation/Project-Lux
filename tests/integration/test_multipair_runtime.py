"""The multi-pair live loop: list[PairContext] behind one process.

Slice 1 of Phase 2 (docs/MULTIPAIR_PLAN.md §5). These tests drive two pairs
through LiveRuntime.for_pairs with fake providers and one shared SQLite file,
pinning the three properties the restructure exists for:

* two pairs advance in one loop pass, each writing rows under its own pair_id;
* a PAUSED pair processes bars but takes no actions while the other trades
  (§5.2 fault isolation);
* no cross-contamination between the two stores on the shared file.

The margin any-pair-open cadence is pinned in test_margin_check.py, next to the
rest of the monitor's tests.
"""

from __future__ import annotations

import io
from dataclasses import replace

from lux_trader.config import AppConfig
from lux_trader.core.models import StrategyState
from lux_trader.runtime.live import LiveDryRunRunner
from lux_trader.runtime.live.engine import LiveRuntime, PairRuntimeSpec
from lux_trader.runtime.live.modes import DryRunLiveModeHandler
from lux_trader.store import SQLiteStore
from lux_trader.terminal_ui import LiveTerminalReporter

from tests.integration.test_live_market_data import (
    dry_run_quote_providers,
    rows,
    small_live_config,
    ts,
)


ENTRY_QUOTE_TIMES = [
    "2026-06-18T08:45:30+08:00",
    "2026-06-18T08:45:59+08:00",
    "2026-06-18T08:46:01+08:00",
    "2026-06-18T08:46:59+08:00",
    "2026-06-18T08:47:01+08:00",
]

CLOCK_TIMES = [
    "2026-06-18T08:45:00+08:00",
    "2026-06-18T08:45:30+08:00",
    "2026-06-18T08:45:59+08:00",
    "2026-06-18T08:46:01+08:00",
    "2026-06-18T08:46:59+08:00",
    "2026-06-18T08:47:01+08:00",
    "2026-06-18T08:47:02+08:00",
]


def repeating_clock(values: list[str]):
    """Like dry_run_clock, but repeats the last timestamp once exhausted.

    Multi-pair runs consume extra clock reads at shutdown (one finish_live_run
    per pair); repeating the final instant keeps the script identical without
    hand-counting internal calls.
    """
    stamps = [ts(value) for value in values]
    state = {"index": 0}

    def clock():
        index = min(state["index"], len(stamps) - 1)
        state["index"] += 1
        return stamps[index]

    return clock


def pair_view(config: AppConfig, pair_id: str, label: str) -> AppConfig:
    """A second pair as another view of the same account config.

    Same store file, same account-level settings; only the pair identity
    differs -- exactly how load_config produces per-pair views of one file.
    """
    pair = replace(config.pairs[0], id=pair_id, label=label)
    return replace(config, pairs=(pair,), active_pair_id=pair_id)


def entry_ready_config(config: AppConfig) -> AppConfig:
    return replace(config, strategy=replace(config.strategy, entry_z=1.0))


def build_spec(config: AppConfig) -> PairRuntimeSpec:
    tw_leg, us_leg, usd = dry_run_quote_providers(list(ENTRY_QUOTE_TIMES))
    return PairRuntimeSpec(
        config=config,
        handler=DryRunLiveModeHandler(config),
        tw_leg_provider=tw_leg,
        us_leg_provider=us_leg,
        usdttwd_provider=usd,
    )


def pair_scoped_counts(store: SQLiteStore, table: str) -> dict[str, int]:
    rows = store.connection.execute(
        f"SELECT pair_id, COUNT(*) AS count FROM {table} GROUP BY pair_id"
    ).fetchall()
    return {str(row["pair_id"]): int(row["count"]) for row in rows}


def test_two_pairs_advance_in_one_loop_and_partition_one_store(tmp_path) -> None:
    config_a = entry_ready_config(small_live_config(tmp_path))
    config_b = pair_view(config_a, "pair_b", "Pair B")

    runtime = LiveRuntime.for_pairs(
        [build_spec(config_a), build_spec(config_b)],
        clock=repeating_clock(CLOCK_TIMES),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    )
    result = runtime.run(reset_store=True, max_iterations=5)

    # Each pair built its two bars and recorded its one entry plan.
    assert result.bars_processed == 4
    assert result.plans_recorded == 2

    store = SQLiteStore(config_a.store_path, **config_a.store_identity())
    try:
        store.initialize()
        assert pair_scoped_counts(store, "bars") == {
            "qff_tsm": 2,
            "pair_b": 2,
        }
        assert pair_scoped_counts(store, "execution_plans") == {
            "qff_tsm": 1,
            "pair_b": 1,
        }
        # live_runs is account-level (no pair_id column); each pair still opens
        # and closes its own run row.
        runs = store.connection.execute(
            "SELECT mode, status FROM live_runs ORDER BY run_id"
        ).fetchall()
        assert len(runs) == 2
        assert all(row["mode"] == "live-dry-run" for row in runs)
        assert all(row["status"] == "stopped" for row in runs)
    finally:
        store.close()

    # Each pair's own view of the shared file sees only its own state.
    for view, expected_state in (
        (config_a, StrategyState.OPEN),
        (config_b, StrategyState.OPEN),
    ):
        view_store = SQLiteStore(view.store_path, **view.store_identity())
        try:
            view_store.initialize()
            resume = view_store.load_resume_state()
            assert resume is not None
            assert resume.strategy.state == expected_state
        finally:
            view_store.close()


def test_paused_pair_idles_while_the_other_still_trades(tmp_path) -> None:
    """§5.2: a pair's PAUSED state stops that pair only.

    Pair B is paused through the persisted-state path (how a real pause
    survives a restart); on the next multi-pair run pair A still enters while
    pair B processes bars without producing a single plan.
    """
    config_a = entry_ready_config(small_live_config(tmp_path))
    config_b = pair_view(config_a, "pair_b", "Pair B")

    # Seed both pairs' persisted state with a first quiet run (high entry_z so
    # both finish FLAT), then flip pair B to PAUSED exactly as a real pause is
    # stored.
    quiet_a = replace(config_a, strategy=replace(config_a.strategy, entry_z=99.0))
    quiet_b = pair_view(quiet_a, "pair_b", "Pair B")
    runtime = LiveRuntime.for_pairs(
        [build_spec(quiet_a), build_spec(quiet_b)],
        clock=repeating_clock(CLOCK_TIMES),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    )
    runtime.run(reset_store=True, max_iterations=5)

    store_b = SQLiteStore(config_b.store_path, **config_b.store_identity())
    try:
        store_b.initialize()
        resume = store_b.load_resume_state()
        assert resume is not None and resume.strategy.state == StrategyState.FLAT
        paused = resume.strategy
        paused.state = StrategyState.PAUSED
        store_b.save_state(
            resume.row_index,
            ts(CLOCK_TIMES[-1]),
            paused,
            resume.indicator,
        )
        store_b.commit()
    finally:
        store_b.close()

    # Same entry pattern two minutes later in the same day session: A enters,
    # B only watches. The resume warmup window is anchored at the new start
    # (08:47), so the fakes must serve warmup bars for the minutes just before
    # it -- the default 04:58-05:00 night-session rows would fall outside it.
    later = [
        "2026-06-18T08:47:30+08:00",
        "2026-06-18T08:47:59+08:00",
        "2026-06-18T08:48:01+08:00",
        "2026-06-18T08:48:59+08:00",
        "2026-06-18T08:49:01+08:00",
    ]
    later_clock = [
        "2026-06-18T08:47:00+08:00",
        *later,
        "2026-06-18T08:49:02+08:00",
    ]
    # Session minutes only: the day session opens at 08:45, so the three
    # session minutes before 08:47 are the night-session tail plus the first
    # two day bars.
    warmup_minutes = [
        "2026-06-18T05:00:00+08:00",
        "2026-06-18T08:45:00+08:00",
        "2026-06-18T08:46:00+08:00",
    ]
    later_warmup = dict(
        tw_leg_rows=rows([(minute, 100.0) for minute in warmup_minutes]),
        us_leg_rows=rows([(minute, 20.0) for minute in warmup_minutes]),
        usd_rows=rows([(minute, 25.0) for minute in warmup_minutes]),
    )
    tw_a, us_a, usd_a = dry_run_quote_providers(later, **later_warmup)
    spec_a = PairRuntimeSpec(
        config=config_a,
        handler=DryRunLiveModeHandler(config_a),
        tw_leg_provider=tw_a,
        us_leg_provider=us_a,
        usdttwd_provider=usd_a,
    )
    tw_b, us_b, usd_b = dry_run_quote_providers(later, **later_warmup)
    spec_b = PairRuntimeSpec(
        config=config_b,
        handler=DryRunLiveModeHandler(config_b),
        tw_leg_provider=tw_b,
        us_leg_provider=us_b,
        usdttwd_provider=usd_b,
    )
    runtime = LiveRuntime.for_pairs(
        [spec_a, spec_b],
        clock=repeating_clock(later_clock),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    )
    result = runtime.run(resume=True, max_iterations=5)

    # Both pairs kept processing bars -- pausing is not stalling.
    assert result.bars_processed == 4
    # Only pair A produced an execution plan.
    store = SQLiteStore(config_a.store_path, **config_a.store_identity())
    try:
        store.initialize()
        assert pair_scoped_counts(store, "execution_plans") == {"qff_tsm": 1}
    finally:
        store.close()

    store_b = SQLiteStore(config_b.store_path, **config_b.store_identity())
    try:
        store_b.initialize()
        resume = store_b.load_resume_state()
        assert resume is not None
        assert resume.strategy.state == StrategyState.PAUSED
    finally:
        store_b.close()


def test_single_pair_via_runner_matches_the_old_entry_flow(tmp_path) -> None:
    """The public runner still drives one pair exactly as before the refactor."""
    config = entry_ready_config(small_live_config(tmp_path))
    tw_leg, us_leg, usd = dry_run_quote_providers(list(ENTRY_QUOTE_TIMES))

    result = LiveDryRunRunner(
        config,
        tw_leg_provider=tw_leg,
        us_leg_provider=us_leg,
        usdttwd_provider=usd,
        clock=repeating_clock(CLOCK_TIMES),
        sleeper=lambda _: None,
        reporter=LiveTerminalReporter(io.StringIO(), color=False),
    ).run(reset_store=True, max_iterations=5)

    assert result.bars_processed == 2
    assert result.plans_recorded == 1
