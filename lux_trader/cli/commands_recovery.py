from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib

from lux_trader.cli import helpers
from lux_trader.config import load_config
from lux_trader.core.models import StrategyState
from lux_trader.presentation import metric_label
from lux_trader.reconciliation import BrokerReconciler, ReadOnlyBroker, ReconciliationStatus
from lux_trader.runtime.live.lease import assert_live_lease_available
from lux_trader.store import SQLiteStore


def command_recover_manual_flat(args: argparse.Namespace) -> int:
    """Record an externally manual-closed pair without inventing fill prices."""
    if bool(args.apply) and not str(args.reason or "").strip():
        raise SystemExit("--reason is required with --apply")
    config = load_config(args.config, pair_id=getattr(args, "pair", None))
    assert_live_lease_available(config.store_path)
    store = SQLiteStore(config.store_path, **config.store_identity())
    brokers: tuple[ReadOnlyBroker, ...] = ()
    try:
        store.initialize()
        latest_run = store.load_latest_live_run()
        if latest_run is not None and latest_run.get("status") == "running":
            raise SystemExit(
                "Refusing recover-manual-flat: latest live run is still running; "
                "stop live-execute gracefully first"
            )
        resume_state = store.load_resume_state()
        if resume_state is None:
            raise SystemExit("No persisted strategy state to recover")
        state = resume_state.strategy
        pending = store.load_pending_manual_close()
        if pending is not None and not strategy_has_position(state):
            print(
                "Manual-flat recovery already applied: "
                f"recovery_id={pending['recovery_id']}, pnl_status=pending"
            )
            return 0
        if state.state != StrategyState.PAUSED:
            raise SystemExit(
                "Refusing recover-manual-flat: strategy must be PAUSED, got "
                f"{state.state.value}"
            )
        if not strategy_has_position(state):
            raise SystemExit(
                "Refusing recover-manual-flat: persisted strategy has no position"
            )
        if state.position_direction is None:
            raise SystemExit(
                "Refusing recover-manual-flat: position direction is missing"
            )

        observed_at = datetime.now().astimezone()
        prospective_state = deepcopy(state)
        clear_strategy_exposure(prospective_state)
        brokers = helpers.build_reconciliation_brokers(
            config,
            prospective_state,
            readonly=bool(args.readonly),
        )
        report = BrokerReconciler(
            us_leg_units_tolerance=config.broker_reconciliation.us_leg_units_tolerance,
            tw_leg_contract_tolerance=config.broker_reconciliation.tw_leg_contract_tolerance,
        ).reconcile(
            strategy_state=prospective_state,
            brokers=brokers,
            us_leg_symbol=config.live.binance_symbol,
            tw_leg_symbol=helpers.reconciliation_tw_leg_symbol(config, state),
            timestamp=observed_at,
        )
        if report.status != ReconciliationStatus.MATCHED:
            print(
                "Refusing recover-manual-flat: prospective flat reconciliation "
                f"status={report.status.value}, issues={len(report.issues)}"
            )
            for issue in report.issues:
                print(
                    f"- {issue.status.value} {issue.issue_type} "
                    f"{issue.broker.value} {issue.symbol or '-'} {issue.message}"
                )
            return 1

        tw_leg_symbol = helpers.reconciliation_tw_leg_symbol(config, state)
        recovery_id = manual_flat_recovery_id(
            row_index=resume_state.row_index,
            us_leg_units=state.us_leg_units,
            tw_leg_contracts=state.tw_leg_contracts,
            tw_leg_symbol=tw_leg_symbol,
        )
        us_adjustment_label = metric_label(
            config.active_pair.us_leg.display, "adjustment"
        )
        tw_adjustment_label = metric_label(
            config.active_pair.tw_leg.display, "adjustment"
        )
        print(
            "Manual-flat recovery verified: "
            f"recovery_id={recovery_id}, "
            f"{us_adjustment_label}={-state.us_leg_units:g}, "
            f"{tw_adjustment_label}={-state.tw_leg_contracts:g}, "
            "brokers=flat, open_orders=0, pnl_status=pending"
        )
        if not args.apply:
            print("Dry-run only; re-run with --apply --reason <reason> to persist")
            return 0

        original_state = deepcopy(state)
        store.record_manual_flat_recovery(
            recovery_id=recovery_id,
            created_at=observed_at,
            row_index=resume_state.row_index,
            tw_leg_symbol=tw_leg_symbol,
            us_leg_symbol=config.live.binance_symbol,
            us_leg_adjustment=-float(state.us_leg_units),
            tw_leg_adjustment=-float(state.tw_leg_contracts),
            reason=str(args.reason).strip(),
            original_state=original_state,
        )
        clear_strategy_exposure(state)
        state.pnl_status = "pending"
        store.save_state(
            resume_state.row_index,
            observed_at,
            state,
            resume_state.indicator,
        )
        store.record_event(
            resume_state.row_index,
            observed_at,
            "manual_flat_recovery",
            "externally manual-closed position reconciled to flat; PnL pending",
            {
                "recovery_id": recovery_id,
                "reason": str(args.reason).strip(),
                "us_leg_adjustment": -float(original_state.us_leg_units),
                "tw_leg_adjustment": -float(original_state.tw_leg_contracts),
                "tw_leg_symbol": tw_leg_symbol,
                "pnl_status": "pending",
            },
        )
        store.commit()
        print(
            "Manual-flat recovery applied; strategy remains PAUSED until "
            "clear-pause completes matched reconciliation"
        )
        return 0
    except Exception:
        store.rollback()
        raise
    finally:
        helpers.close_brokers(brokers)
        store.close()


def strategy_has_position(state: object) -> bool:
    return bool(
        getattr(state, "position_direction", None) is not None
        or abs(float(getattr(state, "us_leg_units", 0.0) or 0.0)) > 1e-12
        or int(getattr(state, "tw_leg_contracts", 0) or 0) != 0
    )


def clear_strategy_exposure(state: object) -> None:
    state.position_direction = None
    state.open_trade = None
    state.us_leg_units = 0.0
    state.tw_leg_units = 0.0
    state.tw_leg_contracts = 0
    state.actual_leg_notional_twd = 0.0
    state.entry_us_leg = None
    state.entry_tw_leg = None
    state.entry_zscore = None
    state.exit_signal_idx = -1
    state.exit_signal_time = None
    state.exit_signal_zscore = None
    state.candidate_direction = None
    state.candidate_idx = -1
    state.candidate_time = None
    state.candidate_zscore = None


def manual_flat_recovery_id(
    *, row_index: int, us_leg_units: float, tw_leg_contracts: int, tw_leg_symbol: str
) -> str:
    identity = f"{row_index}|{us_leg_units:.12g}|{tw_leg_contracts}|{tw_leg_symbol}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"manual-flat-{row_index}-{digest}"


__all__ = ["command_recover_manual_flat"]
