"""The CCF/UMC replay golden.

WHAT THIS PROVES, AND WHAT IT DOES NOT.

It pins the STRATEGY MATHS -- spread, rolling z-score, entry/exit rules, sizing,
fees -- against the PoC run this fixture was frozen from, to the last digit.

It says nothing about LIVE behaviour. The live path scores entries on the
directional bid/ask z-score and fills on the same bar; replay uses the mid/close
z-score and fills on the next bar. A green golden does NOT mean live would take
these trades, and no amount of green here substitutes for a live-semantics
backtest. See docs/CCF_UMC_PLAN.md.

The sample is thin: 18 trades over seven weeks, every one a winner. That record
is a warning rather than a reassurance -- it says only that those seven weeks
contained no adverse move.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lux_trader.config import load_config
from lux_trader.runner import SystemRunner
from lux_trader.store import SQLiteStore


FIXTURE_CONFIG = Path("configs/replay.fixture.ccf_umc.toml")

pytestmark = pytest.mark.skipif(
    not FIXTURE_CONFIG.exists(), reason="CCF/UMC fixture config is unavailable"
)


def build_summary(config):
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        return store.build_summary(config.strategy, config.fees)
    finally:
        store.close()


def run_replay(tmp_path, **strategy_overrides):
    """Load the committed fixture config, redirect the store, replay."""
    config = load_config(FIXTURE_CONFIG)
    config = replace(config, store_path=tmp_path / "replay.sqlite3")
    if strategy_overrides:
        config = replace(config, strategy=replace(config.strategy, **strategy_overrides))
    SystemRunner(config).replay(reset_store=True)
    return config, build_summary(config)


def test_notional_replay_reproduces_the_poc_run(tmp_path) -> None:
    """Frozen from the PoC's `wk_none_0728` run, 2026-07-28.

    Every figure below is the PoC's own output. They match because replay fills
    the way the PoC does -- signalled entries and exits at the NEXT bar's open,
    forced exits at the close. Replay used to fill exits at the close
    unconditionally, which cost 9,449 TWD of the 235,726 here and turned one
    winner into a loser, while looking close enough to pass for alignment.
    """
    config, summary = run_replay(tmp_path)

    assert summary["rows"] == 12_460
    assert summary["parameters"]["zscore_window"] == 2500
    assert summary["parameters"]["entry_z"] == 1.5
    assert summary["parameters"]["exit_z"] == 0.0
    assert summary["parameters"]["ccf_contract_multiplier"] == 2000.0

    assert summary["trade_count"] == 18
    assert summary["winning_trades"] == 18
    assert summary["losing_trades"] == 0
    assert summary["total_pnl_twd"] == pytest.approx(235_726.65723246615)
    assert summary["net_pnl_twd"] == pytest.approx(235_726.65723246636)
    assert summary["gross_pnl_twd"] == pytest.approx(255_861.06235473434)
    assert summary["total_fee_twd"] == pytest.approx(20_134.405122268032)
    assert summary["max_drawdown_twd"] == pytest.approx(-25_585.958856977057)


def test_weekend_rules_are_off_in_the_golden(tmp_path) -> None:
    """CCF/UMC runs weekend_policy='none', and the fixture must exercise that.

    Both venues close over the weekend, so neither the last-session entry ban
    nor the force-close applies. If either had crept back in, these counters
    would be non-zero and the trade set would differ.
    """
    _, summary = run_replay(tmp_path)

    assert summary["friday_night_close_only_minutes"] == 0
    assert summary["weekend_session_close_only_minutes"] == 0
    assert summary["friday_session_end_force_close_minutes"] == 0
    assert summary["entry_allowed_minutes"] == 12_460
    assert summary["close_allowed_minutes"] == 12_460


def test_one_fixed_lot_scales_linearly_from_the_notional_run(tmp_path) -> None:
    """The scale that will actually trade, against the scale that was measured.

    Notional sizing puts on 3 CCF contracts at the median price; live puts on 1.
    The signal is independent of size, and both legs' costs scale with it, so the
    economics should be proportional -- confirming the backtest transfers to the
    size actually traded rather than only to a 1M notional abstraction.
    """
    _, notional = run_replay(tmp_path / "notional")
    _, fixed = run_replay(tmp_path / "fixed", sizing_mode="fixed_lots", ccf_lots=1)

    # Same signals: sizing cannot change which trades the z-score produced.
    assert fixed["trade_count"] == notional["trade_count"] == 18
    assert fixed["winning_trades"] == 18

    # Smaller, and profitable at the smaller size.
    assert 0.0 < fixed["net_pnl_twd"] < notional["net_pnl_twd"]

    # Fees stay a near-constant share of gross rather than eating the smaller
    # position: the drag does not grow as the size shrinks to one lot.
    notional_drag = notional["total_fee_twd"] / notional["gross_pnl_twd"]
    fixed_drag = fixed["total_fee_twd"] / fixed["gross_pnl_twd"]
    assert fixed_drag == pytest.approx(notional_drag, abs=0.005)


def test_resume_replay_matches_a_single_pass(tmp_path) -> None:
    config = load_config(FIXTURE_CONFIG)
    split = replace(config, store_path=tmp_path / "split.sqlite3")

    partial = SystemRunner(split).replay(reset_store=True, max_bars=6_000)
    assert partial.rows_processed == 6_000
    resumed = SystemRunner(split).replay(resume=True)
    assert resumed.rows_processed == 6_460

    _, full = run_replay(tmp_path / "full")
    split_summary = build_summary(split)

    assert split_summary["trade_count"] == full["trade_count"]
    assert split_summary["net_pnl_twd"] == pytest.approx(full["net_pnl_twd"])
    assert split_summary["total_fee_twd"] == pytest.approx(full["total_fee_twd"])
