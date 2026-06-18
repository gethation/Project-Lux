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

    assert result.rows_processed == 60245
    assert summary["trade_count"] == 43
    assert summary["total_pnl_twd"] == pytest.approx(124_992.49304306647)
    assert summary["net_pnl_twd"] == pytest.approx(124_992.4930430663)
    assert summary["total_fee_twd"] == pytest.approx(44_004.5905185781)


def test_resume_replay_matches_single_pass(tmp_path) -> None:
    full_config = make_app_config(tmp_path / "full")
    split_config = make_app_config(tmp_path / "split")

    SystemRunner(full_config).replay(reset_store=True)
    full_summary = build_summary(full_config)

    partial = SystemRunner(split_config).replay(reset_store=True, max_bars=30_000)
    assert partial.rows_processed == 30_000
    resumed = SystemRunner(split_config).replay(resume=True)
    assert resumed.rows_processed == 30_245
    split_summary = build_summary(split_config)

    assert split_summary["trade_count"] == full_summary["trade_count"]
    assert split_summary["total_pnl_twd"] == pytest.approx(full_summary["total_pnl_twd"])
    assert split_summary["total_fee_twd"] == pytest.approx(full_summary["total_fee_twd"])
