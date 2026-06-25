from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from ..reconciliation import ReconciliationReport


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def timestamp_text(value: datetime) -> str:
    return value.isoformat()


class ReconciliationStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def record_report(self, report: ReconciliationReport) -> int:
        report_payload = report.to_jsonable()
        cursor = self.connection.execute(
            """
            INSERT INTO broker_reconciliation_runs (
                timestamp, status, expected_json, report_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                timestamp_text(report.timestamp),
                report.status.value,
                json.dumps(report_payload["expected"], default=json_default),
                json.dumps(report_payload, default=json_default),
            ),
        )
        run_id = int(cursor.lastrowid)
        self.connection.executemany(
            """
            INSERT INTO broker_snapshots (
                run_id, broker, account_id, fetched_at, position_count,
                open_order_count, margin_count, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    snapshot.broker.value,
                    snapshot.account_id,
                    timestamp_text(snapshot.fetched_at),
                    len(snapshot.positions),
                    len(snapshot.open_orders),
                    len(snapshot.margins),
                    json.dumps(snapshot_payload, default=json_default),
                )
                for snapshot, snapshot_payload in zip(
                    report.snapshots,
                    report_payload["snapshots"],
                )
            ],
        )
        self.connection.executemany(
            """
            INSERT INTO broker_reconciliation_issues (
                run_id, status, issue_type, broker, symbol, message,
                expected_quantity, actual_quantity, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    issue.status.value,
                    issue.issue_type,
                    issue.broker.value,
                    issue.symbol,
                    issue.message,
                    issue.expected_quantity,
                    issue.actual_quantity,
                    json.dumps(issue_payload, default=json_default),
                )
                for issue, issue_payload in zip(
                    report.issues,
                    report_payload["issues"],
                )
            ],
        )
        return run_id

    def load_latest_report(self) -> ReconciliationReport | None:
        row = self.connection.execute(
            """
            SELECT report_json
            FROM broker_reconciliation_runs
            ORDER BY run_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return ReconciliationReport.from_jsonable(json.loads(row["report_json"]))
