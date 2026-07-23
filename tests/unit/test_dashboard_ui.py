from __future__ import annotations

import io
from datetime import datetime
from types import SimpleNamespace

from lux_trader.core.models import Direction, StrategyState
from lux_trader.core.strategy import StrategyRuntimeState
from lux_trader.core.tradable_spread import TradableSpreadSnapshot
from lux_trader.dashboard_ui import DashboardReporter


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def snapshot() -> TradableSpreadSnapshot:
    return TradableSpreadSnapshot(
        mid_spread=1.84,
        mid_zscore=1.51,
        short_spread=1.62,
        short_zscore=1.40,
        long_spread=2.06,
        long_zscore=1.93,
    )


def make_reporter() -> tuple[DashboardReporter, io.StringIO]:
    stream = io.StringIO()
    reporter = DashboardReporter(
        mode="live-dry-run",
        tw_leg_display="QFF",
        us_leg_display="TSM",
        tw_leg_symbol="auto",
        binance_symbol="TSM/USDT:USDT",
        bitopro_symbol="USDT/TWD",
        gate_text="allow_live_order=false",
        stream=stream,
        color=False,
    )
    return reporter, stream


def open_state() -> StrategyRuntimeState:
    return StrategyRuntimeState(
        state=StrategyState.OPEN,
        position_direction=Direction.SHORT_US_LONG_TW,
        us_leg_units=-100.0,
        tw_leg_contracts=2,
        entry_zscore=2.14,
        trading_tw_leg_symbol="QFFG6",
        trading_tw_leg_expiry="2026-07-15",
    )


def test_dashboard_absorbs_live_quote_bar_and_position() -> None:
    reporter, _ = make_reporter()
    try:
        reporter.live(ts("2026-06-18T09:12:04+08:00"), snapshot(), open_state())
        reporter.bar(
            ts("2026-06-18T09:13:00+08:00"),
            snapshot(),
            open_state(),
            StrategyState.OPEN and "entry_fill",
            "entry_filled",
            -550.0,
            999_450.0,
        )
    finally:
        reporter.finish()

    state = reporter.state
    assert state.session == "trading"
    assert state.state_text == "SHORT"
    assert state.position_direction == "Short TSM / Long QFF"
    assert state.us_leg_units == -100.0
    assert state.tw_leg_contracts == 2
    # Trading symbol comes from strategy state, replacing the 'auto' placeholder.
    assert state.tw_leg_symbol == "QFFG6"
    assert state.tw_leg_expiry == "2026-07-15"
    assert state.quote_time == "09:12:04"
    assert state.bar_time == "09:13"
    assert state.decision_text == "entry_fill"
    assert state.bar_equity == 999_450.0


def test_dashboard_tracks_session_reconciliation_and_events() -> None:
    reporter, _ = make_reporter()
    try:
        reporter.live_non_trading(
            ts("2026-06-20T02:31:04+08:00"),
            ts("2026-06-22T08:45:00+08:00"),
            "non_trading_session",
        )
        assert reporter.state.session.startswith("non-trading")
        assert reporter.state.next_open_text == "06/22 08:45"

        reporter.event(
            ts("2026-06-20T02:31:05+08:00"),
            "post_trade_reconciliation_matched",
            "run_id=3",
        )
        reporter.warn(ts("2026-06-20T02:31:06+08:00"), "stale_us_leg", "skipped_minute")
        reporter.error(ts("2026-06-20T02:31:07+08:00"), "boom")
    finally:
        reporter.finish()

    state = reporter.state
    assert state.reconciliation_text == "post_trade_reconciliation_matched run_id=3"
    assert state.reconciliation_time == "02:31:05"
    assert state.gate_text == "allow_live_order=false"
    assert len(state.activity) == 3


def test_dashboard_logs_filled_exit_trade_pnl() -> None:
    reporter, _ = make_reporter()
    try:
        reporter.execution(
            ts("2026-06-18T17:30:00+08:00"),
            SimpleNamespace(plan_type="exit"),
            SimpleNamespace(status="filled"),
            SimpleNamespace(
                trade={
                    "net_pnl_twd": -1_234.0,
                    "gross_pnl_twd": -1_000.0,
                    "total_fee_twd": 234.0,
                }
            ),
        )
    finally:
        reporter.finish()

    line = reporter.state.activity[-1]
    assert str(line) == (
        "17:30:00 TRADE EXIT net=-1,234 gross=-1,000 fees=234 TWD"
    )
    assert any(span.style == "red" for span in line.spans)


def test_dashboard_marks_missing_filled_exit_trade_pnl_unavailable() -> None:
    reporter, _ = make_reporter()
    try:
        reporter.execution(
            ts("2026-06-18T17:30:00+08:00"),
            SimpleNamespace(plan_type="exit"),
            SimpleNamespace(status="filled"),
            SimpleNamespace(trade={}),
        )
    finally:
        reporter.finish()

    assert str(reporter.state.activity[-1]).endswith(
        "TRADE EXIT trade_pnl_twd unavailable"
    )


def test_dashboard_margin_panel_tracks_margin_events() -> None:
    reporter, stream = make_reporter()
    try:
        reporter.event(
            ts("2026-07-06T10:00:05+08:00"),
            "margin_check",
            "每日檢查正常 binance=30.0% fubon=30.0% — 不需轉帳。",
        )
        assert reporter.state.margin_level == "ok"

        reporter.warn(
            ts("2026-07-06T10:00:06+08:00"),
            "margin_transfer_required",
            "需要轉帳 binance=10.0% fubon=50.0%",
        )
        assert reporter.state.margin_level == "transfer"
        assert reporter.state.margin_time == "10:00:06"

        reporter.warn(
            ts("2026-07-06T11:15:00+08:00"),
            "margin_red_line",
            "紅線警報 binance=4.0%",
        )
        assert reporter.state.margin_level == "red_line"
    finally:
        reporter.finish()

    output = stream.getvalue()
    assert "Margin" in output
    assert "red_line" in output


def test_dashboard_renders_all_acceptance_fields_to_output() -> None:
    reporter, stream = make_reporter()
    try:
        reporter.live(ts("2026-06-18T09:12:04+08:00"), snapshot(), open_state())
        reporter.event(
            ts("2026-06-18T09:12:05+08:00"),
            "post_trade_reconciliation_matched",
            "",
        )
    finally:
        reporter.finish()

    output = stream.getvalue()
    for expected in (
        "Session",
        "Symbols",
        "QFFG6",
        "TSM/USDT:USDT",
        "USDT/TWD",
        "Gate",
        "Reconcile",
        "QUOTE",
        "BAR",
        "shortSpread",
        "longSpread",
        "State",
        "SHORT",
        "Position",
        "Short TSM / Long QFF",
        "Decision",
        "Activity",
    ):
        assert expected in output, f"missing {expected!r} in dashboard output"
