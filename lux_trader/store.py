from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import FeeConfig, StrategyConfig
from .core.indicator import IndicatorEngine
from .persistence.execution_queries import ExecutionStore
from .persistence.reconciliation_queries import ReconciliationStore
from .persistence.schema import initialize_schema
from .execution.intent import PairExecutionPlan
from .execution.simulation import ExecutionSimulationResult
from .core.models import BrokerName, Fill, IndicatorSnapshot, MarketBar, OrderResult
from .reconciliation import ReconciliationReport
from .core.strategy import StrategyRuntimeState
from .core.tradable_spread import TradableSpreadSnapshot


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def timestamp_text(value: datetime) -> str:
    return value.isoformat()


def display_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S%z").replace(
        "+0800", "+08:00"
    )


@dataclass(frozen=True)
class ResumeState:
    row_index: int
    strategy: StrategyRuntimeState
    indicator: IndicatorEngine


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")

    def close(self) -> None:
        self.connection.close()

    def reset(self) -> None:
        self.connection.close()
        if self.path.exists():
            self.path.unlink()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")

    def initialize(self) -> None:
        initialize_schema(self.connection)
        self.connection.commit()

    def has_bars(self) -> bool:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM bars").fetchone()
        return bool(row["count"])

    def has_warmup_bars(self) -> bool:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM warmup_bars"
        ).fetchone()
        return bool(row["count"])

    def latest_bar_row_index(self) -> int:
        row = self.connection.execute(
            "SELECT MAX(row_index) AS row_index FROM bars"
        ).fetchone()
        if row is None or row["row_index"] is None:
            return -1
        return int(row["row_index"])

    def bar_exists_for_timestamp(self, timestamp: datetime) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM bars WHERE timestamp = ? LIMIT 1",
            (timestamp_text(timestamp),),
        ).fetchone()
        return row is not None

    def load_resume_state(self) -> ResumeState | None:
        row = self.connection.execute(
            "SELECT row_index, state_json, indicator_json FROM strategy_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return ResumeState(
            row_index=int(row["row_index"]),
            strategy=StrategyRuntimeState.from_jsonable(json.loads(row["state_json"])),
            indicator=IndicatorEngine.from_jsonable(json.loads(row["indicator_json"])),
        )

    def save_state(
        self,
        row_index: int,
        timestamp: datetime,
        strategy: StrategyRuntimeState,
        indicator: IndicatorEngine,
    ) -> None:
        indicator_payload = {"window": indicator.window}
        self.connection.execute(
            """
            INSERT INTO strategy_state (
                id, row_index, timestamp, state_json, indicator_json, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                row_index = excluded.row_index,
                timestamp = excluded.timestamp,
                state_json = excluded.state_json,
                indicator_json = excluded.indicator_json,
                updated_at = excluded.updated_at
            """,
            (
                row_index,
                timestamp_text(timestamp),
                json.dumps(strategy.to_jsonable(), default=json_default),
                json.dumps(indicator_payload, default=json_default),
                timestamp_text(datetime.now(timestamp.tzinfo)),
            ),
        )

    def record_event(
        self,
        row_index: int,
        timestamp: datetime,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO events (
                row_index, timestamp, event_type, message, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                row_index,
                timestamp_text(timestamp),
                event_type,
                message,
                json.dumps(payload or {}, default=json_default),
            ),
        )

    def record_order(self, order: OrderResult) -> None:
        request = order.request
        self.connection.execute(
            """
            INSERT OR REPLACE INTO orders (
                order_id, row_index, timestamp, broker, symbol, side,
                quantity, price, status, qff_symbol, qff_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_id,
                request.row_index,
                timestamp_text(request.timestamp),
                request.broker.value,
                request.symbol,
                request.side.value,
                request.quantity,
                request.price,
                order.status.value,
                request.qff_symbol,
                request.qff_expiry,
                request.contract_policy_state,
                json.dumps(
                    {
                        "order_id": order.order_id,
                        "fee_twd": request.fee_twd,
                        "order_type": request.order_type,
                        "expected_price": request.expected_price,
                        "trigger_bid": request.trigger_bid,
                        "trigger_ask": request.trigger_ask,
                        "trigger_mid": request.trigger_mid,
                        "price_source": request.price_source,
                    },
                    default=json_default,
                ),
            ),
        )

    def record_fill(self, fill: Fill) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO fills (
                fill_id, order_id, row_index, timestamp, broker, symbol,
                side, quantity, price, fee_twd, qff_symbol, qff_expiry,
                contract_policy_state, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.fill_id,
                fill.order_id,
                fill.row_index,
                timestamp_text(fill.timestamp),
                fill.broker.value,
                fill.symbol,
                fill.side.value,
                fill.quantity,
                fill.price,
                fill.fee_twd,
                fill.qff_symbol,
                fill.qff_expiry,
                fill.contract_policy_state,
                json.dumps(
                    {"fill_id": fill.fill_id, "actual_fill_price": fill.price},
                    default=json_default,
                ),
            ),
        )

    def load_recorded_fill_exposure(
        self,
        *,
        tsm_symbol: str,
        qff_symbol: str,
    ) -> dict[BrokerName, float]:
        rows = self.connection.execute(
            """
            SELECT broker, SUM(quantity) AS quantity
            FROM (
                SELECT broker, symbol,
                       CASE WHEN side = 'buy' THEN quantity ELSE -quantity END AS quantity
                FROM fills
                UNION ALL
                SELECT broker, symbol, quantity
                FROM position_adjustments
            ) exposure_rows
            WHERE (broker = ? AND symbol = ?)
               OR (broker = ? AND symbol = ?)
            GROUP BY broker
            """,
            (
                BrokerName.BINANCE_TSM.value,
                tsm_symbol,
                BrokerName.FUBON_QFF.value,
                qff_symbol,
            ),
        ).fetchall()
        exposure = {
            BrokerName.BINANCE_TSM: 0.0,
            BrokerName.FUBON_QFF: 0.0,
        }
        for row in rows:
            exposure[BrokerName(str(row["broker"]))] = float(row["quantity"] or 0.0)
        return exposure

    def load_latest_live_run(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM live_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def load_pending_manual_close(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM pending_manual_closes
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["original_state"] = json.loads(result.pop("original_state_json"))
        return result

    def record_manual_flat_recovery(
        self,
        *,
        recovery_id: str,
        created_at: datetime,
        row_index: int,
        qff_symbol: str,
        tsm_symbol: str,
        tsm_adjustment: float,
        qff_adjustment: float,
        reason: str,
        original_state: StrategyRuntimeState,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO pending_manual_closes (
                recovery_id, created_at, settled_at, status, row_index,
                qff_symbol, reason, original_state_json, settlement_json
            ) VALUES (?, ?, NULL, 'pending', ?, ?, ?, ?, NULL)
            """,
            (
                recovery_id,
                timestamp_text(created_at),
                row_index,
                qff_symbol,
                reason,
                json.dumps(original_state.to_jsonable(), default=json_default),
            ),
        )
        adjustments = (
            (
                f"{recovery_id}:BINANCE_TSM",
                BrokerName.BINANCE_TSM.value,
                tsm_symbol,
                float(tsm_adjustment),
            ),
            (
                f"{recovery_id}:FUBON_QFF",
                BrokerName.FUBON_QFF.value,
                qff_symbol,
                float(qff_adjustment),
            ),
        )
        for adjustment_id, broker, symbol, quantity in adjustments:
            self.connection.execute(
                """
                INSERT INTO position_adjustments (
                    adjustment_id, recovery_id, created_at, broker, symbol,
                    quantity, reason, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    adjustment_id,
                    recovery_id,
                    timestamp_text(created_at),
                    broker,
                    symbol,
                    quantity,
                    reason,
                    json.dumps(
                        {
                            "source": "external_manual_close",
                            "price_status": "unknown",
                            "pnl_status": "pending",
                        }
                    ),
                ),
            )

    def record_trade(self, trade: dict[str, Any]) -> None:
        payload = {
            key: timestamp_text(value) if isinstance(value, datetime) else value
            for key, value in trade.items()
        }
        columns = [
            "entry_signal_idx",
            "entry_signal_time",
            "entry_signal_zscore",
            "entry_idx",
            "entry_time",
            "entry_delay_minutes",
            "entry_fill_zscore",
            "direction",
            "entry_tsm_twd_fair",
            "entry_qff_close",
            "tsm_units",
            "qff_units",
            "qff_contracts",
            "raw_qff_contracts",
            "leg_notional_twd",
            "actual_leg_notional_twd",
            "qff_contract_multiplier",
            "entry_tsm_fee_twd",
            "entry_qff_fee_twd",
            "entry_qff_tax_twd",
            "entry_fee_twd",
            "exit_signal_idx",
            "exit_signal_time",
            "exit_signal_zscore",
            "exit_idx",
            "exit_time",
            "exit_fill_zscore",
            "exit_tsm_twd_fair",
            "exit_qff_close",
            "tsm_pnl",
            "qff_pnl",
            "gross_pnl_twd",
            "exit_tsm_fee_twd",
            "exit_qff_fee_twd",
            "exit_qff_tax_twd",
            "exit_fee_twd",
            "tsm_fee_twd",
            "qff_fee_twd",
            "qff_tax_twd",
            "total_fee_twd",
            "net_pnl_twd",
            "total_pnl",
            "exit_reason",
            "holding_minutes",
            "qff_symbol",
            "qff_expiry",
            "contract_policy_state",
        ]
        values = [payload.get(column) for column in columns]
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"""
            INSERT INTO trades ({", ".join(columns)}, payload_json)
            VALUES ({placeholders}, ?)
            """,
            values + [json.dumps(payload, default=json_default)],
        )

    def record_bar(
        self,
        bar: MarketBar,
        snapshot: IndicatorSnapshot,
        strategy: StrategyRuntimeState,
        unrealized_pnl: float,
        equity: float,
        running_max_equity: float,
        drawdown_twd: float,
        drawdown_pct: float,
        tradable_snapshot: TradableSpreadSnapshot | None = None,
        decision_spread_type: str | None = None,
        decision_zscore: float | None = None,
    ) -> None:
        position = strategy.position_direction.value if strategy.position_direction else "flat"
        short_spread = tradable_snapshot.short_spread if tradable_snapshot else None
        short_zscore = tradable_snapshot.short_zscore if tradable_snapshot else None
        long_spread = tradable_snapshot.long_spread if tradable_snapshot else None
        long_zscore = tradable_snapshot.long_zscore if tradable_snapshot else None
        self.connection.execute(
            """
            INSERT OR REPLACE INTO bars (
                row_index, timestamp, spread, spread_mean, spread_std,
                spread_zscore, zscore_valid, entry_allowed, close_allowed,
                friday_night_close_only, weekend_session_close_only,
                friday_session_end_force_close, qff_close_filled, tsm_twd_fair,
                qff_was_filled, qff_entry_price, tsm_entry_twd_fair,
                qff_entry_open_was_filled,
                qff_symbol, qff_expiry, contract_policy_state,
                short_spread, short_zscore, long_spread, long_zscore,
                decision_spread_type, decision_zscore,
                state, position, tsm_units, qff_units, qff_contracts,
                actual_leg_notional_twd, realized_pnl, realized_fee_twd,
                unrealized_pnl, equity, running_max_equity, drawdown_twd,
                drawdown_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bar.row_index,
                timestamp_text(bar.timestamp),
                snapshot.spread,
                snapshot.mean,
                snapshot.std,
                snapshot.zscore,
                int(snapshot.zscore_valid),
                int(snapshot.entry_allowed),
                int(snapshot.close_allowed),
                int(snapshot.friday_night_close_only),
                int(snapshot.weekend_session_close_only),
                int(snapshot.friday_session_end_force_close),
                bar.qff_close_filled,
                bar.tsm_twd_fair,
                int(bar.qff_was_filled),
                bar.qff_entry_price,
                bar.tsm_entry_twd_fair,
                int(bar.qff_entry_open_was_filled),
                bar.qff_symbol,
                bar.qff_expiry,
                bar.contract_policy_state,
                short_spread,
                short_zscore,
                long_spread,
                long_zscore,
                decision_spread_type,
                decision_zscore,
                strategy.state.value,
                position,
                strategy.tsm_units,
                strategy.qff_units,
                strategy.qff_contracts,
                strategy.actual_leg_notional_twd,
                strategy.realized_pnl,
                strategy.realized_fee_twd,
                unrealized_pnl,
                equity,
                running_max_equity,
                drawdown_twd,
                drawdown_pct,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO positions (
                row_index, timestamp, state, direction, tsm_units, qff_units,
                qff_contracts, actual_leg_notional_twd, realized_pnl,
                unrealized_pnl, equity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bar.row_index,
                timestamp_text(bar.timestamp),
                strategy.state.value,
                position,
                strategy.tsm_units,
                strategy.qff_units,
                strategy.qff_contracts,
                strategy.actual_leg_notional_twd,
                strategy.realized_pnl,
                unrealized_pnl,
                equity,
            ),
        )

    def record_market_tick(self, quote: Any, observed_at: datetime) -> None:
        raw = getattr(quote, "raw", None) or {}
        self.connection.execute(
            """
            INSERT INTO market_ticks (
                observed_at, source, symbol, quote_timestamp, price, bid, ask, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp_text(observed_at),
                str(quote.source),
                str(quote.symbol),
                timestamp_text(quote.timestamp),
                float(quote.price),
                quote.bid,
                quote.ask,
                json.dumps(raw, default=json_default),
            ),
        )

    def record_warmup_bars(self, bars: list[MarketBar]) -> None:
        self.connection.executemany(
            """
            INSERT OR REPLACE INTO warmup_bars (
                timestamp, qff_close, qff_close_filled, tsm_twd_fair, spread,
                qff_was_filled, qff_symbol, qff_expiry, contract_policy_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    timestamp_text(bar.timestamp),
                    bar.qff_close,
                    bar.qff_close_filled,
                    bar.tsm_twd_fair,
                    bar.spread,
                    int(bar.qff_was_filled),
                    bar.qff_symbol,
                    bar.qff_expiry,
                    bar.contract_policy_state,
                )
                for bar in bars
            ],
        )

    def replace_warmup_bars(self, bars: list[MarketBar]) -> None:
        self.connection.execute("DELETE FROM warmup_bars")
        self.record_warmup_bars(bars)

    def load_indicator_seed_bars(
        self,
        limit: int,
        *,
        qff_symbol: str | None = None,
    ) -> list[MarketBar]:
        rows = []
        warmup_where = ""
        warmup_params: tuple[Any, ...] = ()
        bars_where = ""
        bars_params: tuple[Any, ...] = ()
        if qff_symbol is not None:
            warmup_where = "WHERE qff_symbol = ?"
            warmup_params = (qff_symbol,)
            bars_where = "WHERE qff_symbol = ?"
            bars_params = (qff_symbol,)
        rows.extend(
            self.connection.execute(
                f"""
                SELECT timestamp, qff_close, qff_close_filled, tsm_twd_fair, spread,
                       qff_was_filled, qff_symbol, qff_expiry, contract_policy_state
                FROM warmup_bars
                {warmup_where}
                """,
                warmup_params,
            ).fetchall()
        )
        rows.extend(
            self.connection.execute(
                f"""
                SELECT timestamp, qff_close_filled AS qff_close,
                       qff_close_filled, tsm_twd_fair, spread,
                       qff_was_filled, qff_symbol, qff_expiry, contract_policy_state
                FROM bars
                {bars_where}
                """,
                bars_params,
            ).fetchall()
        )
        by_timestamp: dict[str, sqlite3.Row] = {str(row["timestamp"]): row for row in rows}
        ordered = sorted(by_timestamp.items(), key=lambda item: item[0])[-limit:]
        return [
            MarketBar(
                row_index=index - len(ordered),
                timestamp=datetime.fromisoformat(timestamp),
                qff_close=row["qff_close"],
                qff_close_filled=float(row["qff_close_filled"]),
                tsm_twd_fair=float(row["tsm_twd_fair"]),
                spread=float(row["spread"]),
                qff_was_filled=bool(row["qff_was_filled"]),
                qff_symbol=row["qff_symbol"],
                qff_expiry=row["qff_expiry"],
                contract_policy_state=row["contract_policy_state"],
            )
            for index, (timestamp, row) in enumerate(ordered)
        ]

    def load_latest_qff_close_filled(self, *, qff_symbol: str | None = None) -> float | None:
        warmup_where = ""
        warmup_params: tuple[Any, ...] = ()
        bars_where = ""
        bars_params: tuple[Any, ...] = ()
        if qff_symbol is not None:
            warmup_where = "WHERE qff_symbol = ?"
            warmup_params = (qff_symbol,)
            bars_where = "WHERE qff_symbol = ?"
            bars_params = (qff_symbol,)
        rows = self.connection.execute(
            f"""
            SELECT qff_close_filled
            FROM (
                SELECT timestamp, qff_close_filled FROM warmup_bars {warmup_where}
                UNION ALL
                SELECT timestamp, qff_close_filled FROM bars {bars_where}
            )
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (*warmup_params, *bars_params),
        ).fetchone()
        if rows is None or rows["qff_close_filled"] is None:
            return None
        return float(rows["qff_close_filled"])

    def start_live_run(
        self,
        *,
        started_at: datetime,
        mode: str,
        qff_symbol: str | None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO live_runs (
                started_at, mode, qff_symbol, status, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                timestamp_text(started_at),
                mode,
                qff_symbol,
                "running",
                json.dumps(payload or {}, default=json_default),
            ),
        )
        return int(cursor.lastrowid)

    def finish_live_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE live_runs
            SET finished_at = ?, status = ?, payload_json = ?
            WHERE run_id = ?
            """,
            (
                timestamp_text(finished_at),
                status,
                json.dumps(payload or {}, default=json_default),
                run_id,
            ),
        )

    def record_reconciliation_report(self, report: ReconciliationReport) -> int:
        return ReconciliationStore(self.connection).record_report(report)

    def load_latest_reconciliation_report(self) -> ReconciliationReport | None:
        return ReconciliationStore(self.connection).load_latest_report()

    def record_margin_check(self, decision: Any) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO margin_checks (
                checked_at, check_type,
                binance_equity, binance_maint_margin, binance_ratio,
                fubon_equity, fubon_maint_margin, fubon_ratio,
                usdttwd_rate, level, transfer_amount_twd, transfer_direction,
                guidance, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp_text(decision.checked_at),
                decision.check_type,
                decision.binance.equity_twd,
                decision.binance.maint_margin_twd,
                decision.binance.ratio,
                decision.fubon.equity_twd,
                decision.fubon.maint_margin_twd,
                decision.fubon.ratio,
                decision.usdttwd_rate,
                decision.level,
                decision.transfer_amount_twd,
                decision.transfer_direction,
                decision.guidance,
                json.dumps(decision.payload, default=json_default),
            ),
        )
        return int(cursor.lastrowid)

    def load_last_margin_check(
        self, check_type: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM margin_checks"
        params: tuple[Any, ...] = ()
        if check_type is not None:
            query += " WHERE check_type = ?"
            params = (check_type,)
        query += " ORDER BY check_id DESC LIMIT 1"
        row = self.connection.execute(query, params).fetchone()
        return dict(row) if row is not None else None

    def record_execution_plan(self, plan: PairExecutionPlan) -> None:
        ExecutionStore(self.connection).record_plan(plan)

    def load_latest_execution_plan_payload(self) -> dict[str, Any] | None:
        return ExecutionStore(self.connection).load_latest_plan_payload()

    def execution_plan_has_outcome(self, plan_id: str) -> bool:
        return ExecutionStore(self.connection).plan_has_outcome(plan_id)

    def record_execution_simulation(
        self,
        result: ExecutionSimulationResult,
    ) -> int:
        return ExecutionStore(self.connection).record_simulation(result)

    def record_execution_outcome(self, outcome: Any) -> int:
        return ExecutionStore(self.connection).record_outcome(outcome)

    def record_fubon_session_health(
        self,
        *,
        observed_at: datetime,
        health: dict[str, Any],
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO fubon_session_events (
                observed_at, role, generation, worker_pid, status,
                last_login_at, last_success_at, relogin_count,
                invalid_reason, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp_text(observed_at),
                str(health.get("role") or "trading"),
                int(health.get("generation") or 0),
                health.get("worker_pid"),
                str(health.get("status") or "unknown"),
                timestamp_text(health["last_login_at"])
                if isinstance(health.get("last_login_at"), datetime)
                else health.get("last_login_at"),
                timestamp_text(health["last_success_at"])
                if isinstance(health.get("last_success_at"), datetime)
                else health.get("last_success_at"),
                int(health.get("relogin_count") or 0),
                health.get("invalid_reason"),
                json.dumps(health, default=json_default),
            ),
        )
        return int(cursor.lastrowid)

    def load_latest_fubon_session_health(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM fubon_session_events
            WHERE role = 'trading'
            ORDER BY session_event_id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def load_latest_execution_simulation_payload(self) -> dict[str, Any] | None:
        return ExecutionStore(self.connection).load_latest_simulation_payload()

    def build_execution_summary(self) -> dict[str, Any]:
        return ExecutionStore(self.connection).build_summary()

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def build_summary(self, strategy: StrategyConfig, fees: FeeConfig) -> dict[str, Any]:
        bar_count = self.connection.execute("SELECT COUNT(*) AS count FROM bars").fetchone()[
            "count"
        ]
        if bar_count == 0:
            return {
                "rows": 0,
                "trade_count": 0,
                "total_pnl_twd": 0.0,
                "final_equity_twd": strategy.initial_capital_twd,
            }

        bar_stats = self.connection.execute(
            """
            SELECT
                MIN(timestamp) AS start,
                MAX(timestamp) AS end,
                SUM(entry_allowed) AS entry_allowed_minutes,
                SUM(close_allowed) AS close_allowed_minutes,
                SUM(friday_night_close_only) AS friday_night_close_only_minutes,
                SUM(weekend_session_close_only) AS weekend_session_close_only_minutes,
                SUM(friday_session_end_force_close) AS friday_session_end_force_close_minutes,
                SUM(qff_was_filled) AS qff_forward_filled_session_minutes,
                SUM(CASE WHEN position != 'flat' THEN 1 ELSE 0 END) AS exposure_minutes,
                MIN(drawdown_twd) AS max_drawdown_twd,
                MIN(drawdown_pct) AS max_drawdown_pct
            FROM bars
            """
        ).fetchone()
        last_bar = self.connection.execute(
            "SELECT equity FROM bars ORDER BY row_index DESC LIMIT 1"
        ).fetchone()
        trade_stats = self.connection.execute(
            """
            SELECT
                COUNT(*) AS trade_count,
                SUM(CASE WHEN total_pnl > 0 THEN 1 ELSE 0 END) AS winning_trades,
                SUM(CASE WHEN total_pnl < 0 THEN 1 ELSE 0 END) AS losing_trades,
                SUM(CASE WHEN total_pnl > 0 THEN total_pnl ELSE 0 END) AS gross_profit_twd,
                SUM(CASE WHEN total_pnl < 0 THEN total_pnl ELSE 0 END) AS gross_loss_twd,
                SUM(gross_pnl_twd) AS gross_pnl_twd,
                SUM(net_pnl_twd) AS net_pnl_twd,
                SUM(total_fee_twd) AS total_fee_twd,
                SUM(tsm_fee_twd) AS total_tsm_fee_twd,
                SUM(qff_fee_twd) AS total_qff_fee_twd,
                SUM(qff_tax_twd) AS total_qff_tax_twd,
                SUM(CASE WHEN exit_reason = 'friday_session_end' THEN 1 ELSE 0 END) AS friday_session_forced_exits,
                SUM(holding_minutes) AS exposure_elapsed_minutes,
                AVG(total_pnl) AS avg_trade_pnl_twd
            FROM trades
            """
        ).fetchone()
        trade_count = int(trade_stats["trade_count"] or 0)
        final_equity = float(last_bar["equity"])
        total_pnl = final_equity - strategy.initial_capital_twd
        gross_loss = float(trade_stats["gross_loss_twd"] or 0.0)
        gross_profit = float(trade_stats["gross_profit_twd"] or 0.0)
        start_text = display_timestamp(bar_stats["start"])
        end_text = display_timestamp(bar_stats["end"])
        elapsed_minutes = 0
        if bar_stats["start"] and bar_stats["end"]:
            elapsed_minutes = int(
                (
                    datetime.fromisoformat(bar_stats["end"])
                    - datetime.fromisoformat(bar_stats["start"])
                ).total_seconds()
                // 60
            )
        exposure_elapsed = int(trade_stats["exposure_elapsed_minutes"] or 0)

        return {
            "fee_defaults_as_of": "2026-06-17",
            "parameters": {
                "entry_z": strategy.entry_z,
                "exit_z": strategy.exit_z,
                "zscore_window": strategy.zscore_window,
                "leg_notional_twd": strategy.leg_notional_twd,
                "qff_lots": strategy.qff_lots,
                "initial_capital_twd": strategy.initial_capital_twd,
                "max_entry_delay_minutes": strategy.max_entry_delay_minutes,
                "tsm_fee_bps": fees.tsm_fee_bps,
                "qff_fee_per_contract_twd": fees.qff_fee_per_contract_twd,
                "qff_tax_rate": fees.qff_tax_rate,
                "qff_contract_multiplier": fees.qff_contract_multiplier,
            },
            "rows": int(bar_count),
            "start": start_text,
            "end": end_text,
            "entry_allowed_minutes": int(bar_stats["entry_allowed_minutes"] or 0),
            "close_allowed_minutes": int(bar_stats["close_allowed_minutes"] or 0),
            "friday_night_close_only_minutes": int(
                bar_stats["friday_night_close_only_minutes"] or 0
            ),
            "weekend_session_close_only_minutes": int(
                bar_stats["weekend_session_close_only_minutes"] or 0
            ),
            "friday_session_end_force_close_minutes": int(
                bar_stats["friday_session_end_force_close_minutes"] or 0
            ),
            "qff_forward_filled_session_minutes": int(
                bar_stats["qff_forward_filled_session_minutes"] or 0
            ),
            "trade_count": trade_count,
            "friday_session_forced_exits": int(
                trade_stats["friday_session_forced_exits"] or 0
            ),
            "winning_trades": int(trade_stats["winning_trades"] or 0),
            "losing_trades": int(trade_stats["losing_trades"] or 0),
            "win_rate": float((trade_stats["winning_trades"] or 0) / trade_count)
            if trade_count
            else 0.0,
            "total_pnl_twd": total_pnl,
            "gross_pnl_twd": float(trade_stats["gross_pnl_twd"] or 0.0),
            "net_pnl_twd": float(trade_stats["net_pnl_twd"] or 0.0),
            "total_fee_twd": float(trade_stats["total_fee_twd"] or 0.0),
            "total_tsm_fee_twd": float(trade_stats["total_tsm_fee_twd"] or 0.0),
            "total_qff_fee_twd": float(trade_stats["total_qff_fee_twd"] or 0.0),
            "total_qff_tax_twd": float(trade_stats["total_qff_tax_twd"] or 0.0),
            "return_pct": float(total_pnl / strategy.initial_capital_twd),
            "gross_profit_twd": gross_profit,
            "gross_loss_twd": gross_loss,
            "profit_factor": float(gross_profit / abs(gross_loss)) if gross_loss else None,
            "avg_trade_pnl_twd": float(trade_stats["avg_trade_pnl_twd"] or 0.0),
            "max_drawdown_twd": float(bar_stats["max_drawdown_twd"] or 0.0),
            "max_drawdown_pct": float(bar_stats["max_drawdown_pct"] or 0.0),
            "elapsed_minutes": elapsed_minutes,
            "exposure_minutes": exposure_elapsed,
            "exposure_ratio": float(exposure_elapsed / elapsed_minutes)
            if elapsed_minutes
            else 0.0,
            "final_equity_twd": final_equity,
        }
