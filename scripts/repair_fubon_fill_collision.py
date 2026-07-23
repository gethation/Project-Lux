"""Repair a Fubon fill-ledger collision after a stale order-row match.

Dry-run is the default.  ``--apply`` refuses to run while the latest live run
is still marked running, creates a SQLite online backup, repairs both collided
normalized records, and verifies that recorded fill exposure matches the
persisted strategy state.  This script never changes strategy state or sends
broker requests/orders; ``clear-pause`` remains a separate guarded step.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any


FUBON = "FUBON"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--prior-plan-id", required=True)
    parser.add_argument("--current-plan-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the repair; otherwise only validate and print the proposal",
    )
    return parser.parse_args()


def load_store_path(config_path: Path) -> Path:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    store_path = Path(config["paths"]["store_path"])
    if not store_path.is_absolute():
        store_path = (config_path.resolve().parent.parent / store_path).resolve()
    return store_path


def load_outcome(connection: sqlite3.Connection, plan_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload_json FROM execution_outcomes WHERE plan_id = ?",
        (plan_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Execution outcome not found: {plan_id}")
    return json.loads(row["payload_json"])


def fubon_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    try:
        return outcome["payload"]["primary_outcomes"][FUBON]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("Outcome has no primary Fubon execution payload") from exc


def first_text(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def current_fubon_position_price(
    connection: sqlite3.Connection,
    *,
    symbol: str,
) -> float:
    rows = connection.execute(
        "SELECT report_json FROM broker_reconciliation_runs ORDER BY run_id DESC"
    ).fetchall()
    for row in rows:
        report = json.loads(row["report_json"])
        for snapshot in report.get("snapshots", []):
            if snapshot.get("broker") != FUBON:
                continue
            for position in snapshot.get("positions", []):
                if position.get("symbol") != symbol:
                    continue
                raw = position.get("raw") or {}
                value = raw.get("price") or raw.get("average_price")
                if value is not None and float(value) > 0:
                    return float(value)
    raise RuntimeError(f"No reconciled Fubon position price found for {symbol}")


def normalized_order(order: dict[str, Any], *, order_id: str) -> tuple[Any, ...]:
    request = order["request"]
    payload = {
        "order_id": order_id,
        "fee_twd": request.get("fee_twd", 0.0),
        "order_type": request.get("order_type"),
        "expected_price": request.get("expected_price"),
        "trigger_bid": request.get("trigger_bid"),
        "trigger_ask": request.get("trigger_ask"),
        "trigger_mid": request.get("trigger_mid"),
        "price_source": request.get("price_source"),
    }
    return (
        order_id,
        request["row_index"],
        request["timestamp"],
        request["broker"],
        request["symbol"],
        request["side"],
        request["quantity"],
        request["price"],
        order["status"],
        request.get("tw_leg_symbol"),
        request.get("tw_leg_expiry"),
        request.get("contract_policy_state"),
        json.dumps(payload),
    )


def normalized_fill(
    fill: dict[str, Any],
    *,
    order_id: str,
    price: float | None = None,
) -> tuple[Any, ...]:
    fill_id = f"FUBON-FILL-{order_id}"
    actual_price = float(price if price is not None else fill["price"])
    return (
        fill_id,
        order_id,
        fill["row_index"],
        fill["timestamp"],
        fill["broker"],
        fill["symbol"],
        fill["side"],
        fill["quantity"],
        actual_price,
        fill["fee_twd"],
        fill.get("tw_leg_symbol"),
        fill.get("tw_leg_expiry"),
        fill.get("contract_policy_state"),
        json.dumps({"fill_id": fill_id, "actual_fill_price": actual_price}),
    )


def recorded_exposure(connection: sqlite3.Connection, symbol: str) -> float:
    row = connection.execute(
        """
        SELECT SUM(CASE WHEN side = 'buy' THEN quantity ELSE -quantity END)
        FROM fills WHERE broker = ? AND symbol = ?
        """,
        (FUBON, symbol),
    ).fetchone()
    return float(row[0] or 0.0)


def main() -> int:
    args = parse_args()
    store_path = load_store_path(args.config)
    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    try:
        state_row = connection.execute(
            "SELECT state_json, row_index, timestamp FROM strategy_state WHERE id = 1"
        ).fetchone()
        if state_row is None:
            raise RuntimeError("Persisted strategy state is missing")
        state = json.loads(state_row["state_json"])
        if state.get("state") != "paused":
            raise RuntimeError(f"Expected PAUSED state, found {state.get('state')}")

        prior = fubon_outcome(load_outcome(connection, args.prior_plan_id))
        current = fubon_outcome(load_outcome(connection, args.current_plan_id))
        prior_order, prior_fill = prior["orders"][0], prior["fills"][0]
        current_order, current_fill = current["orders"][0], current["fills"][0]
        prior_id = str(prior_order["order_id"])
        collided_id = str(current_order["order_id"])
        place_result = current["payload"]["place_result"]
        current_id = first_text(
            place_result,
            "order_no",
            "orderNo",
            "ord_no",
            "seq_no",
            "seqNo",
        )
        if not current_id:
            raise RuntimeError("Current place_result has no broker order identifier")
        if prior_id != collided_id or current_id == collided_id:
            raise RuntimeError(
                "Outcomes do not show the expected old/new Fubon ID collision: "
                f"prior={prior_id}, recorded_current={collided_id}, actual_current={current_id}"
            )
        if prior_order["request"]["symbol"] != current_order["request"]["symbol"]:
            raise RuntimeError("The collided Fubon orders use different symbols")

        symbol = str(current_order["request"]["symbol"])
        actual_price = current_fubon_position_price(connection, symbol=symbol)
        before = recorded_exposure(connection, symbol)
        expected = float(state.get("tw_leg_contracts") or 0.0)
        print(f"store={store_path}")
        print(f"state=paused expected_tw_leg={expected:g} recorded_tw_leg={before:g}")
        print(
            "repair="
            f"restore {prior_id} row={prior_fill['row_index']}; "
            f"insert {current_id} row={current_fill['row_index']} price={actual_price:g}"
        )

        latest_run = connection.execute(
            "SELECT run_id, status, finished_at FROM live_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if not args.apply:
            print("dry_run=passed (no database changes)")
            if latest_run and latest_run["status"] == "running":
                print("apply_blocked=latest live run is still running; stop it gracefully first")
            return 0
        if latest_run and (
            latest_run["status"] == "running" or latest_run["finished_at"] is None
        ):
            raise RuntimeError("Refusing repair while the latest live run is still running")

        backup_path = store_path.with_name(
            f"{store_path.stem}.pre-fill-repair-"
            f"{datetime.now().astimezone():%Y%m%d-%H%M%S}{store_path.suffix}"
        )
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
        finally:
            backup.close()

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT OR REPLACE INTO orders (
                order_id, row_index, timestamp, broker, symbol, side,
                quantity, price, status, tw_leg_symbol, tw_leg_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_order(prior_order, order_id=prior_id),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO fills (
                fill_id, order_id, row_index, timestamp, broker, symbol,
                side, quantity, price, fee_twd, tw_leg_symbol, tw_leg_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_fill(prior_fill, order_id=prior_id),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO orders (
                order_id, row_index, timestamp, broker, symbol, side,
                quantity, price, status, tw_leg_symbol, tw_leg_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_order(current_order, order_id=current_id),
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO fills (
                fill_id, order_id, row_index, timestamp, broker, symbol,
                side, quantity, price, fee_twd, tw_leg_symbol, tw_leg_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            normalized_fill(current_fill, order_id=current_id, price=actual_price),
        )
        after = recorded_exposure(connection, symbol)
        if abs(after - expected) > 1e-12:
            raise RuntimeError(
                f"Repair verification failed: expected={expected} recorded={after}"
            )
        connection.execute(
            """
            INSERT INTO events (row_index, timestamp, event_type, message, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                state_row["row_index"],
                datetime.now().astimezone().isoformat(),
                "manual_fill_ledger_repair",
                "repaired collided Fubon order/fill identifiers",
                json.dumps(
                    {
                        "prior_plan_id": args.prior_plan_id,
                        "current_plan_id": args.current_plan_id,
                        "prior_order_id": prior_id,
                        "current_order_id": current_id,
                        "recorded_tw_leg_before": before,
                        "recorded_tw_leg_after": after,
                        "backup_path": str(backup_path),
                    }
                ),
            ),
        )
        connection.commit()
        print(f"backup={backup_path}")
        print(f"applied=passed recorded_tw_leg={after:g}")
        return 0
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
