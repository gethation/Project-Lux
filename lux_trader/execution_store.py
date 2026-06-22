from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .execution_intent import PairExecutionPlan
from .execution_simulator import ExecutionSimulationResult


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def timestamp_text(value: datetime) -> str:
    return value.isoformat()


class ExecutionStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def record_plan(self, plan: PairExecutionPlan) -> None:
        payload = plan.to_jsonable()
        self.connection.execute(
            "DELETE FROM execution_legs WHERE plan_id = ?",
            (plan.plan_id,),
        )
        self.connection.execute(
            "DELETE FROM execution_checks WHERE plan_id = ?",
            (plan.plan_id,),
        )
        self.connection.execute(
            """
            INSERT OR REPLACE INTO execution_plans (
                plan_id, row_index, timestamp, plan_type, direction, status,
                reason, decision_zscore, decision_spread_type, qff_symbol,
                qff_expiry, contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                plan.row_index,
                timestamp_text(plan.timestamp),
                plan.plan_type.value,
                plan.direction.value,
                plan.status.value,
                plan.reason,
                plan.decision_zscore,
                plan.decision_spread_type,
                plan.qff_symbol,
                plan.qff_expiry,
                plan.contract_policy_state,
                json.dumps(payload, default=json_default),
            ),
        )
        self.connection.executemany(
            """
            INSERT INTO execution_legs (
                plan_id, row_index, timestamp, broker, symbol, side, quantity,
                price, fee_twd, qff_symbol, qff_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    plan.plan_id,
                    leg.row_index,
                    timestamp_text(leg.timestamp),
                    leg.broker.value,
                    leg.symbol,
                    leg.side.value,
                    leg.quantity,
                    leg.price,
                    leg.fee_twd,
                    leg.qff_symbol,
                    leg.qff_expiry,
                    leg.contract_policy_state,
                    json.dumps(leg_payload, default=json_default),
                )
                for leg, leg_payload in zip(plan.legs, payload["legs"])
            ],
        )
        self.connection.executemany(
            """
            INSERT INTO execution_checks (
                plan_id, check_type, passed, broker, symbol, message, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    plan.plan_id,
                    check.check_type,
                    int(check.passed),
                    check.broker.value if check.broker else None,
                    check.symbol,
                    check.message,
                    json.dumps(check_payload, default=json_default),
                )
                for check, check_payload in zip(plan.checks, payload["checks"])
            ],
        )

    def load_latest_plan_payload(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT payload_json
            FROM execution_plans
            ORDER BY timestamp DESC, row_index DESC, plan_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def plan_has_outcome(self, plan_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM execution_outcomes
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone()
        return bool(row["count"] if row is not None else 0)

    def record_simulation(self, result: ExecutionSimulationResult) -> int:
        payload = result.to_jsonable()
        cursor = self.connection.execute(
            """
            INSERT INTO execution_simulations (
                plan_id, timestamp, scenario, status, broker, symbol, message,
                recommended_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.plan_id,
                timestamp_text(result.timestamp),
                result.scenario.value,
                result.status.value,
                result.broker.value if result.broker else None,
                result.symbol,
                result.message,
                result.recommended_state,
                json.dumps(payload, default=json_default),
            ),
        )
        return int(cursor.lastrowid)

    def record_outcome(self, outcome: Any) -> int:
        payload = outcome.to_jsonable()
        recommended_state = getattr(outcome, "recommended_state", None)
        cursor = self.connection.execute(
            """
            INSERT INTO execution_outcomes (
                plan_id, timestamp, status, message, recommended_state,
                payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.plan_id,
                timestamp_text(outcome.timestamp),
                outcome.status.value,
                outcome.message,
                recommended_state.value if recommended_state else None,
                json.dumps(payload, default=json_default),
            ),
        )
        return int(cursor.lastrowid)

    def load_latest_simulation_payload(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT payload_json
            FROM execution_simulations
            ORDER BY simulation_id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"])

    def build_summary(self) -> dict[str, Any]:
        plan_count = self._count("execution_plans")
        leg_count = self._count("execution_legs")
        check_count = self._count("execution_checks")
        failed_check_count = int(
            self.connection.execute(
                "SELECT COUNT(*) AS count FROM execution_checks WHERE passed = 0"
            ).fetchone()["count"]
            or 0
        )
        simulation_count = self._count("execution_simulations")
        outcome_count = self._count("execution_outcomes")
        status_rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM execution_plans
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        outcome_status_rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM execution_outcomes
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        latest = self.connection.execute(
            """
            SELECT plan_id, timestamp, row_index, plan_type, direction, status,
                   reason, qff_symbol
            FROM execution_plans
            ORDER BY timestamp DESC, row_index DESC, plan_id DESC
            LIMIT 1
            """
        ).fetchone()
        table_counts = {
            table: self._count(table)
            for table in ("orders", "fills", "trades")
        }
        return {
            "plan_count": plan_count,
            "leg_count": leg_count,
            "check_count": check_count,
            "failed_check_count": failed_check_count,
            "simulation_count": simulation_count,
            "outcome_count": outcome_count,
            "status_counts": {
                str(row["status"]): int(row["count"]) for row in status_rows
            },
            "outcome_status_counts": {
                str(row["status"]): int(row["count"])
                for row in outcome_status_rows
            },
            "latest_plan": dict(latest) if latest is not None else None,
            "orders": table_counts["orders"],
            "fills": table_counts["fills"],
            "trades": table_counts["trades"],
        }

    def _count(self, table: str) -> int:
        return int(
            self.connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}"
            ).fetchone()["count"]
            or 0
        )
