from __future__ import annotations

import sqlite3


SCHEMA_VERSION = 2


class StoreSchemaVersionError(RuntimeError):
    pass


SQLITE_SCHEMA = r"""
            CREATE TABLE IF NOT EXISTS pairs (
                pair_id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                tw_leg_display TEXT NOT NULL,
                us_leg_display TEXT NOT NULL,
                tw_leg_venue TEXT NOT NULL,
                us_leg_venue TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strategy_state (
                pair_id TEXT PRIMARY KEY,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                state_json TEXT NOT NULL,
                indicator_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                pair_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                status TEXT NOT NULL,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY,
                pair_id TEXT NOT NULL,
                order_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                fee_twd REAL NOT NULL,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(order_id) REFERENCES orders(order_id)
            );

            CREATE TABLE IF NOT EXISTS positions (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                state TEXT NOT NULL,
                direction TEXT,
                us_leg_units REAL NOT NULL,
                tw_leg_units REAL NOT NULL,
                tw_leg_contracts INTEGER NOT NULL,
                actual_leg_notional_twd REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                equity REAL NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS bars (
                pair_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                spread REAL NOT NULL,
                spread_mean REAL,
                spread_std REAL,
                spread_zscore REAL,
                zscore_valid INTEGER NOT NULL,
                entry_allowed INTEGER NOT NULL,
                close_allowed INTEGER NOT NULL,
                friday_night_close_only INTEGER NOT NULL,
                weekend_session_close_only INTEGER NOT NULL DEFAULT 0,
                friday_session_end_force_close INTEGER NOT NULL DEFAULT 0,
                tw_leg_close_filled REAL NOT NULL,
                us_leg_twd_fair REAL NOT NULL,
                tw_leg_was_filled INTEGER NOT NULL DEFAULT 0,
                tw_leg_entry_price REAL,
                us_leg_entry_twd_fair REAL,
                tw_leg_entry_open_was_filled INTEGER NOT NULL DEFAULT 0,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                short_spread REAL,
                short_zscore REAL,
                long_spread REAL,
                long_zscore REAL,
                decision_spread_type TEXT,
                decision_zscore REAL,
                state TEXT NOT NULL,
                position TEXT NOT NULL,
                us_leg_units REAL NOT NULL,
                tw_leg_units REAL NOT NULL,
                tw_leg_contracts INTEGER NOT NULL,
                actual_leg_notional_twd REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                realized_fee_twd REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                equity REAL NOT NULL,
                running_max_equity REAL NOT NULL,
                drawdown_twd REAL NOT NULL,
                drawdown_pct REAL NOT NULL,
                PRIMARY KEY(pair_id, row_index),
                UNIQUE(pair_id, timestamp),
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                entry_signal_idx INTEGER NOT NULL,
                entry_signal_time TEXT NOT NULL,
                entry_signal_zscore REAL,
                entry_idx INTEGER NOT NULL,
                entry_time TEXT NOT NULL,
                entry_delay_minutes INTEGER NOT NULL,
                entry_fill_zscore REAL,
                direction TEXT NOT NULL,
                entry_us_leg_twd_fair REAL NOT NULL,
                entry_tw_leg_close REAL NOT NULL,
                us_leg_units REAL NOT NULL,
                tw_leg_units REAL NOT NULL,
                tw_leg_contracts INTEGER NOT NULL,
                raw_tw_leg_contracts REAL NOT NULL,
                leg_notional_twd REAL NOT NULL,
                actual_leg_notional_twd REAL NOT NULL,
                tw_leg_contract_multiplier REAL NOT NULL,
                entry_us_leg_fee_twd REAL NOT NULL,
                entry_tw_leg_fee_twd REAL NOT NULL,
                entry_tw_leg_tax_twd REAL NOT NULL,
                entry_fee_twd REAL NOT NULL,
                exit_signal_idx INTEGER NOT NULL,
                exit_signal_time TEXT NOT NULL,
                exit_signal_zscore REAL,
                exit_idx INTEGER NOT NULL,
                exit_time TEXT NOT NULL,
                exit_fill_zscore REAL,
                exit_us_leg_twd_fair REAL NOT NULL,
                exit_tw_leg_close REAL NOT NULL,
                us_leg_pnl REAL NOT NULL,
                tw_leg_pnl REAL NOT NULL,
                gross_pnl_twd REAL NOT NULL,
                exit_us_leg_fee_twd REAL NOT NULL,
                exit_tw_leg_fee_twd REAL NOT NULL,
                exit_tw_leg_tax_twd REAL NOT NULL,
                exit_fee_twd REAL NOT NULL,
                us_leg_fee_twd REAL NOT NULL,
                tw_leg_fee_twd REAL NOT NULL,
                tw_leg_tax_twd REAL NOT NULL,
                total_fee_twd REAL NOT NULL,
                net_pnl_twd REAL NOT NULL,
                total_pnl REAL NOT NULL,
                exit_reason TEXT NOT NULL,
                holding_minutes INTEGER NOT NULL,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS market_ticks (
                tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quote_timestamp TEXT NOT NULL,
                price REAL NOT NULL,
                bid REAL,
                ask REAL,
                raw_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS warmup_bars (
                pair_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                tw_leg_close REAL,
                tw_leg_close_filled REAL NOT NULL,
                us_leg_twd_fair REAL NOT NULL,
                spread REAL NOT NULL,
                tw_leg_was_filled INTEGER NOT NULL DEFAULT 0,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                PRIMARY KEY(pair_id, timestamp),
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS live_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                mode TEXT NOT NULL,
                tw_leg_symbol TEXT,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS broker_reconciliation_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                expected_json TEXT NOT NULL,
                report_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS broker_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                broker TEXT NOT NULL,
                account_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                position_count INTEGER NOT NULL,
                open_order_count INTEGER NOT NULL,
                margin_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES broker_reconciliation_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS broker_reconciliation_issues (
                issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                pair_id TEXT,
                status TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT,
                message TEXT NOT NULL,
                expected_quantity REAL,
                actual_quantity REAL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(run_id) REFERENCES broker_reconciliation_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS execution_plans (
                plan_id TEXT PRIMARY KEY,
                pair_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                plan_type TEXT NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                decision_zscore REAL,
                decision_spread_type TEXT,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS execution_legs (
                leg_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                fee_twd REAL NOT NULL,
                tw_leg_symbol TEXT,
                tw_leg_expiry TEXT,
                contract_policy_state TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS execution_checks (
                check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                check_type TEXT NOT NULL,
                passed INTEGER NOT NULL,
                broker TEXT,
                symbol TEXT,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS execution_simulations (
                simulation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                scenario TEXT NOT NULL,
                status TEXT NOT NULL,
                broker TEXT,
                symbol TEXT,
                message TEXT NOT NULL,
                recommended_state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS execution_outcomes (
                outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                recommended_state TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(plan_id) REFERENCES execution_plans(plan_id)
            );

            CREATE TABLE IF NOT EXISTS pending_manual_closes (
                recovery_id TEXT PRIMARY KEY,
                pair_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                settled_at TEXT,
                status TEXT NOT NULL,
                row_index INTEGER NOT NULL,
                tw_leg_symbol TEXT NOT NULL,
                reason TEXT NOT NULL,
                original_state_json TEXT NOT NULL,
                settlement_json TEXT,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS position_adjustments (
                adjustment_id TEXT PRIMARY KEY,
                pair_id TEXT NOT NULL,
                recovery_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                broker TEXT NOT NULL,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(recovery_id, broker, symbol),
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(recovery_id) REFERENCES pending_manual_closes(recovery_id)
            );

            CREATE INDEX IF NOT EXISTS idx_position_adjustments_exposure
            ON position_adjustments(broker, symbol);

            CREATE TABLE IF NOT EXISTS fubon_order_attempts (
                attempt_id TEXT PRIMARY KEY,
                pair_id TEXT NOT NULL,
                plan_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
            );

            CREATE TABLE IF NOT EXISTS fubon_evidence_events (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                accepted INTEGER NOT NULL,
                rejection_reason TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(pair_id) REFERENCES pairs(pair_id),
                FOREIGN KEY(attempt_id) REFERENCES fubon_order_attempts(attempt_id)
            );

            CREATE INDEX IF NOT EXISTS idx_fubon_evidence_attempt
            ON fubon_evidence_events(attempt_id, evidence_id);

            CREATE TABLE IF NOT EXISTS fubon_session_events (
                session_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                observed_at TEXT NOT NULL,
                role TEXT NOT NULL,
                generation INTEGER NOT NULL,
                worker_pid INTEGER,
                status TEXT NOT NULL,
                last_login_at TEXT,
                last_success_at TEXT,
                relogin_count INTEGER NOT NULL,
                invalid_reason TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_fubon_session_events_latest
            ON fubon_session_events(role, session_event_id DESC);

            CREATE TABLE IF NOT EXISTS margin_checks (
                check_id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL,
                check_type TEXT NOT NULL,
                binance_equity REAL,
                binance_maint_margin REAL,
                binance_ratio REAL,
                fubon_equity REAL,
                fubon_maint_margin REAL,
                fubon_ratio REAL,
                usdttwd_rate REAL,
                level TEXT NOT NULL,
                transfer_amount_twd REAL,
                transfer_direction TEXT,
                guidance TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

"""


def initialize_schema(connection: sqlite3.Connection) -> None:
    validate_schema_compatibility(connection)
    connection.executescript(SQLITE_SCHEMA)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def validate_schema_compatibility(connection: sqlite3.Connection) -> None:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    if not tables:
        return
    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current_version != SCHEMA_VERSION:
        raise StoreSchemaVersionError(
            "Project Lux store schema is incompatible "
            f"(found version {current_version}, required {SCHEMA_VERSION}). "
            "Archive this store and create a new one; in-place migration is not supported."
        )
