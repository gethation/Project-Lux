from __future__ import annotations

import pytest

from lux_trader.runner import SystemRunner
from lux_trader.store import SQLiteStore

from conftest import POC_CSV, make_app_config


pytestmark = pytest.mark.skipif(not POC_CSV.exists(), reason="PoC CSV is unavailable")


def build_summary(config):
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        return store.build_summary(config.strategy, config.fees)
    finally:
        store.close()


def test_full_poc_replay_matches_reference_summary(tmp_path) -> None:
    config = make_app_config(tmp_path)

    result = SystemRunner(config).replay(reset_store=True)
    summary = build_summary(config)

    assert result.rows_processed == 29909
    assert summary["parameters"]["zscore_window"] == 500
    assert summary["parameters"]["exit_z"] == 1.0
    assert summary["trade_count"] == 66
    assert summary["total_pnl_twd"] == pytest.approx(265_481.3183435681)
    assert summary["net_pnl_twd"] == pytest.approx(265_481.318343568)
    assert summary["total_fee_twd"] == pytest.approx(68_321.48561792255)
    assert summary["qff_forward_filled_session_minutes"] == 6328
    assert summary["weekend_session_close_only_minutes"] == 4872


def test_resume_replay_matches_single_pass(tmp_path) -> None:
    full_config = make_app_config(tmp_path / "full")
    split_config = make_app_config(tmp_path / "split")

    SystemRunner(full_config).replay(reset_store=True)
    full_summary = build_summary(full_config)

    partial = SystemRunner(split_config).replay(reset_store=True, max_bars=15_000)
    assert partial.rows_processed == 15_000
    resumed = SystemRunner(split_config).replay(resume=True)
    assert resumed.rows_processed == 14_909
    split_summary = build_summary(split_config)

    assert split_summary["trade_count"] == full_summary["trade_count"]
    assert split_summary["total_pnl_twd"] == pytest.approx(full_summary["total_pnl_twd"])
    assert split_summary["total_fee_twd"] == pytest.approx(full_summary["total_fee_twd"])
