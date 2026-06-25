from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from conftest import make_app_config

from lux_trader.execution.gate import evaluate_live_execution_gate
from lux_trader.reconciliation import (
    BrokerOrderSnapshot,
    ExpectedBrokerState,
    ReconciliationIssue,
    ReconciliationReport,
    ReconciliationStatus,
)
from lux_trader.core.models import BrokerName, OrderSide
from lux_trader.cli_helpers import build_fake_execution_plan


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def live_enabled_config(tmp_path):
    base = make_app_config(tmp_path, validate_expected_zscore=False)
    return replace(
        base,
        safety=replace(base.safety, allow_live_order=True),
        live=replace(base.live, qff_symbol="QFFG6"),
        live_execution=replace(base.live_execution, enabled=True),
    )


def required_env() -> dict[str, str]:
    return {
        "PROJECT_LUX_ALLOW_LIVE_ORDER": "1",
        "FUBON_ALLOW_LIVE_ORDER": "1",
        "BINANCE_ALLOW_LIVE_ORDER": "1",
    }


def matched_report(config, timestamp: datetime) -> ReconciliationReport:
    return ReconciliationReport(
        timestamp=timestamp,
        status=ReconciliationStatus.MATCHED,
        expected=ExpectedBrokerState(
            timestamp=timestamp,
            tsm_symbol=config.live.binance_symbol,
            qff_symbol=config.live.qff_symbol,
            expected_tsm_units=0.0,
            expected_qff_contracts=0,
        ),
        snapshots=(),
        issues=(),
    )


def report_with_open_order(config, timestamp: datetime) -> ReconciliationReport:
    issue = ReconciliationIssue(
        status=ReconciliationStatus.WARNING,
        issue_type="unexpected_open_order",
        broker=BrokerName.FUBON_QFF,
        symbol=config.live.qff_symbol,
        message="FUBON_QFF has open order TEST QFFG6",
        payload={
            "order": BrokerOrderSnapshot(
                broker=BrokerName.FUBON_QFF,
                order_id="TEST",
                symbol=config.live.qff_symbol,
                side=OrderSide.BUY,
                quantity=1,
                status="open",
            ).raw
        },
    )
    return ReconciliationReport(
        timestamp=timestamp,
        status=ReconciliationStatus.WARNING,
        expected=matched_report(config, timestamp).expected,
        snapshots=(),
        issues=(issue,),
    )


def fake_plan(config, timestamp: datetime):
    return build_fake_execution_plan(
        config,
        fake_case="valid",
        timestamp=timestamp,
        row_index=1,
    )


def failed_check_types(report) -> set[str]:
    return {check.check_type for check in report.failed_checks}


def test_live_execution_gate_closed_by_default(tmp_path) -> None:
    config = make_app_config(tmp_path, validate_expected_zscore=False)
    now = ts("2026-06-22T09:00:00+08:00")

    report = evaluate_live_execution_gate(config, environ={}, now=now)

    assert not report.passed
    failures = failed_check_types(report)
    assert "safety_allow_live_order" in failures
    assert "live_execution_enabled" in failures
    assert "env_PROJECT_LUX_ALLOW_LIVE_ORDER" in failures
    assert "readonly_reconciliation_present" in failures
    assert "execution_plan_present" in failures


def test_live_execution_gate_opens_with_matched_reconciliation_and_fresh_plan(
    tmp_path,
) -> None:
    config = live_enabled_config(tmp_path)
    now = ts("2026-06-22T09:00:00+08:00")

    report = evaluate_live_execution_gate(
        config,
        environ=required_env(),
        reconciliation_report=matched_report(config, now),
        plan=fake_plan(config, now),
        now=now,
    )

    assert report.passed
    assert report.failed_checks == ()


def test_live_execution_gate_rejects_stale_plan(tmp_path) -> None:
    config = live_enabled_config(tmp_path)
    now = ts("2026-06-22T09:00:00+08:00")
    stale_plan = fake_plan(config, now - timedelta(seconds=121))

    report = evaluate_live_execution_gate(
        config,
        environ=required_env(),
        reconciliation_report=matched_report(config, now),
        plan=stale_plan,
        now=now,
    )

    assert "execution_plan_fresh" in failed_check_types(report)


def test_live_execution_gate_rejects_previously_executed_plan(tmp_path) -> None:
    config = live_enabled_config(tmp_path)
    now = ts("2026-06-22T09:00:00+08:00")

    report = evaluate_live_execution_gate(
        config,
        environ=required_env(),
        reconciliation_report=matched_report(config, now),
        plan=fake_plan(config, now),
        plan_has_outcome=True,
        now=now,
    )

    assert "execution_plan_not_executed" in failed_check_types(report)


def test_live_execution_gate_rejects_open_orders_from_reconciliation(
    tmp_path,
) -> None:
    config = live_enabled_config(tmp_path)
    now = ts("2026-06-22T09:00:00+08:00")

    report = evaluate_live_execution_gate(
        config,
        environ=required_env(),
        reconciliation_report=report_with_open_order(config, now),
        plan=fake_plan(config, now),
        now=now,
    )

    failures = failed_check_types(report)
    assert "readonly_reconciliation_matched" in failures
    assert "no_unexpected_open_orders" in failures
