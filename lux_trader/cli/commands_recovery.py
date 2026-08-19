from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib

from lux_trader.cli import helpers
from lux_trader.config import load_config
from lux_trader.core.models import BrokerName, StrategyState
from lux_trader.reconciliation import BrokerReconciler, ReadOnlyBroker, ReconciliationStatus
from lux_trader.runtime.live.lease import assert_live_lease_available
from lux_trader.store import SQLiteStore


def command_recover_manual_flat(args: argparse.Namespace) -> int:
    """Record an externally manual-closed pair without inventing fill prices."""
    if bool(args.apply) and not str(args.reason or "").strip():
        raise SystemExit("--reason is required with --apply")
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    store = SQLiteStore(config.store_path)
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
        # A recovery that already ran still has to be checked, not waved past.
        # The adjustment it wrote used to be derived from the strategy rather
        # than the ledger, so an earlier run can have left recorded exposure
        # off-square -- and that silently blocks clear-pause forever, with the
        # operator holding no tool to correct it. Re-verify, and re-square if
        # there is anything left over.
        already_applied = pending is not None and not strategy_has_position(state)
        if not already_applied:
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
            umc_units_tolerance=config.broker_reconciliation.umc_units_tolerance,
            ccf_contract_tolerance=config.broker_reconciliation.ccf_contract_tolerance,
        ).reconcile(
            strategy_state=prospective_state,
            brokers=brokers,
            umc_symbol=config.live.umc_symbol,
            ccf_symbol=helpers.reconciliation_ccf_symbol(config, state),
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

        ccf_symbol = helpers.reconciliation_ccf_symbol(config, state)
        recovery_id = manual_flat_recovery_id(
            row_index=resume_state.row_index,
            umc_units=state.umc_units,
            ccf_contracts=state.ccf_contracts,
            ccf_symbol=ccf_symbol,
        )
        # Correct the LEDGER against the brokers, not against the strategy.
        #
        # These two disagree exactly when a pair half-completes, which is the
        # only situation this command exists for. On 2026-08-19 the CCF exit
        # filled and the UMC exit did not, so the ledger was already square on
        # CCF while the strategy still believed it held the lot. Mirroring the
        # strategy (-state.ccf_contracts) wrote a second -1 onto a balanced
        # ledger and left recorded exposure at -1 against a flat broker --
        # which then refused clear-pause indefinitely, with no tool able to
        # undo it.
        #
        # The reconciliation above has already MATCHED the brokers against a
        # flat prospective state, so flat is what they hold and zero is what
        # the ledger has to reach.
        recorded = store.load_recorded_fill_exposure(
            umc_symbol=config.live.umc_symbol,
            ccf_symbol=ccf_symbol,
        )
        # + 0.0 normalises IEEE negative zero, so a ledger that is already
        # square prints "0" rather than "-0" to the operator reading it.
        umc_adjustment = -float(recorded.get(BrokerName.IBKR_UMC, 0.0)) + 0.0
        ccf_adjustment = -float(recorded.get(BrokerName.FUBON_CCF, 0.0)) + 0.0
        if already_applied:
            assert pending is not None
            if abs(umc_adjustment) <= 1e-12 and abs(ccf_adjustment) <= 1e-12:
                print(
                    "Manual-flat recovery already applied: "
                    f"recovery_id={pending['recovery_id']}, "
                    "ledger square, brokers flat, pnl_status=pending"
                )
                return 0
            print(
                "Manual-flat recovery already applied but the ledger is NOT "
                f"square: recovery_id={pending['recovery_id']}, "
                f"recorded=(umc={recorded.get(BrokerName.IBKR_UMC, 0.0):g}, "
                f"ccf={recorded.get(BrokerName.FUBON_CCF, 0.0):g}) "
                "against flat brokers; "
                f"repair umc_adjustment={umc_adjustment:g}, "
                f"fubon_adjustment={ccf_adjustment:g}"
            )
            if not args.apply:
                print(
                    "Dry-run only; re-run with --apply --reason <reason> to persist"
                )
                return 0
            store.record_manual_flat_ledger_repair(
                recovery_id=str(pending["recovery_id"]),
                created_at=observed_at,
                ccf_symbol=ccf_symbol,
                umc_symbol=config.live.umc_symbol,
                umc_adjustment=umc_adjustment,
                ccf_adjustment=ccf_adjustment,
                reason=str(args.reason).strip(),
            )
            store.record_event(
                resume_state.row_index,
                observed_at,
                "manual_flat_ledger_repair",
                "recorded-fill ledger re-squared against flat brokers",
                {
                    "recovery_id": str(pending["recovery_id"]),
                    "reason": str(args.reason).strip(),
                    "umc_adjustment": umc_adjustment,
                    "ccf_adjustment": ccf_adjustment,
                    "ccf_symbol": ccf_symbol,
                },
            )
            store.commit()
            print("Ledger repair applied; clear-pause can now reconcile")
            return 0

        print(
            "Manual-flat recovery verified: "
            f"recovery_id={recovery_id}, "
            f"umc_adjustment={umc_adjustment:g}, "
            f"fubon_adjustment={ccf_adjustment:g}, "
            f"recorded_before=(umc={recorded.get(BrokerName.IBKR_UMC, 0.0):g}, "
            f"ccf={recorded.get(BrokerName.FUBON_CCF, 0.0):g}), "
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
            ccf_symbol=ccf_symbol,
            umc_symbol=config.live.umc_symbol,
            umc_adjustment=umc_adjustment,
            ccf_adjustment=ccf_adjustment,
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
                "umc_adjustment": umc_adjustment,
                "ccf_adjustment": ccf_adjustment,
                "ccf_symbol": ccf_symbol,
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
        or abs(float(getattr(state, "umc_units", 0.0) or 0.0)) > 1e-12
        or int(getattr(state, "ccf_contracts", 0) or 0) != 0
    )


def clear_strategy_exposure(state: object) -> None:
    state.position_direction = None
    state.open_trade = None
    state.umc_units = 0.0
    state.ccf_units = 0.0
    state.ccf_contracts = 0
    state.actual_leg_notional_twd = 0.0
    state.entry_umc = None
    state.entry_ccf = None
    state.entry_zscore = None
    state.exit_signal_idx = -1
    state.exit_signal_time = None
    state.exit_signal_zscore = None
    state.candidate_direction = None
    state.candidate_idx = -1
    state.candidate_time = None
    state.candidate_zscore = None


def manual_flat_recovery_id(
    *, row_index: int, umc_units: float, ccf_contracts: int, ccf_symbol: str
) -> str:
    identity = f"{row_index}|{umc_units:.12g}|{ccf_contracts}|{ccf_symbol}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"manual-flat-{row_index}-{digest}"


__all__ = ["command_recover_manual_flat"]
