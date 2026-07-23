from __future__ import annotations

import sqlite3

import pytest

from lux_trader.persistence.schema import SCHEMA_VERSION, StoreSchemaVersionError
from lux_trader.store import SQLiteStore


PAIR_SCOPED_TABLES = {
    "strategy_state",
    "events",
    "orders",
    "fills",
    "positions",
    "bars",
    "trades",
    "market_ticks",
    "warmup_bars",
    "execution_plans",
    "execution_legs",
    "execution_checks",
    "execution_simulations",
    "execution_outcomes",
    "pending_manual_closes",
    "position_adjustments",
    "fubon_order_attempts",
    "fubon_evidence_events",
}

ACCOUNT_SCOPED_TABLES = {
    "margin_checks",
    "broker_reconciliation_runs",
    "broker_snapshots",
    "fubon_session_events",
}


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def test_schema_scopes_pair_and_account_tables_exactly(tmp_path) -> None:
    path = tmp_path / "pair-scoped.sqlite3"
    store = SQLiteStore(
        path,
        pair_id="qff_tsm",
        pair_label="QFF/TSM",
        tw_leg_display="QFF",
        us_leg_display="TSM",
        tw_leg_venue="fubon",
        us_leg_venue="binance",
    )
    try:
        store.initialize()
        pair = store.connection.execute(
            "SELECT * FROM pairs WHERE pair_id = ?",
            ("qff_tsm",),
        ).fetchone()

        assert pair is not None
        assert dict(pair) == {
            "pair_id": "qff_tsm",
            "label": "QFF/TSM",
            "tw_leg_display": "QFF",
            "us_leg_display": "TSM",
            "tw_leg_venue": "fubon",
            "us_leg_venue": "binance",
        }
        for table in PAIR_SCOPED_TABLES:
            assert "pair_id" in column_names(store.connection, table), table
        for table in ACCOUNT_SCOPED_TABLES:
            assert "pair_id" not in column_names(store.connection, table), table
        assert "pair_id" in column_names(
            store.connection, "broker_reconciliation_issues"
        )
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        store.close()


def test_old_store_is_rejected_without_recreation_or_upgrade(tmp_path) -> None:
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE strategy_state (id INTEGER PRIMARY KEY CHECK (id = 1))"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(
        StoreSchemaVersionError,
        match=(
            r"found version 0, required 2.*"
            r"Archive this store and create a new one; in-place migration is not supported"
        ),
    ):
        SQLiteStore(path)

    connection = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert tables == {"strategy_state"}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        connection.close()
