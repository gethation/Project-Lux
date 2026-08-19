from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib

from lux_trader.cli import helpers
from lux_trader.config import load_config
from lux_trader.core.fees import fill_costs
from lux_trader.core.models import BrokerName, OrderSide, StrategyState
from lux_trader.core.sizing import umc_contract_twd_price
from lux_trader.core.strategy import minutes_between
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


def command_settle_manual_close(args: argparse.Namespace) -> int:
    """Book the PnL of a manual close that `recover manual-flat` left pending.

    manual-flat deliberately squares the ledger WITHOUT inventing fill prices,
    which leaves realized_pnl short by one round trip and pnl_status pending
    forever -- there was no way back to complete. This is that way back.

    The UMC leg is the hard half. A leg closed by hand outside this system has
    no fill anywhere in the store, and its execution is not reachable over the
    API either: IBKR returns executions only to the client that placed them,
    unless that client holds the Gateway's master client id. What IS reachable
    is the account's realized PnL, which IBKR reports net of real commissions
    -- a better number than any fee model, and one nobody has to type in.
    """
    if bool(args.apply) and not str(args.reason or "").strip():
        raise SystemExit("--reason is required with --apply")
    if not args.from_broker and args.umc_exit_price is None:
        raise SystemExit(
            "Nothing to settle from: pass --from-broker to read IBKR's realized "
            "PnL for today, or --umc-exit-price with the fill from your own "
            "record (IBKR keeps executions for the current trading day only, so "
            "a close settled later has to come from your statement)"
        )
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    store = SQLiteStore(config.store_path)
    broker = None
    try:
        store.initialize()
        latest_run = store.load_latest_live_run()
        if latest_run is not None and latest_run.get("status") == "running":
            raise SystemExit(
                "Refusing settle-manual-close: latest live run is still running; "
                "the engine would overwrite this state on its next bar"
            )
        pending = store.load_pending_manual_close()
        if pending is None:
            raise SystemExit("No pending manual close to settle")
        resume_state = store.load_resume_state()
        if resume_state is None:
            raise SystemExit("No persisted strategy state to settle against")
        state = resume_state.strategy
        if state.pnl_status == "complete":
            raise SystemExit(
                "Refusing settle-manual-close: pnl_status is already complete"
            )
        if strategy_has_position(state):
            raise SystemExit(
                "Refusing settle-manual-close: the persisted strategy still "
                "holds a position; run `recover manual-flat --apply` first"
            )
        open_trade = dict(pending["original_state"].get("open_trade") or {})
        if not open_trade:
            raise SystemExit(
                "Refusing settle-manual-close: the recorded recovery carries no "
                "open trade, so there is nothing to price"
            )

        observed_at = datetime.now().astimezone()
        entry_idx = int(open_trade["entry_idx"])
        exit_idx = int(pending["row_index"])
        umc_units = float(open_trade["umc_units"])
        ccf_units = float(open_trade["ccf_units"])
        ccf_contracts = int(open_trade["ccf_contracts"])

        usd_twd, fx_source = resolve_settlement_fx(
            store, args, created_at=pending["created_at"]
        )
        ccf_exit_price, ccf_source = resolve_ccf_exit_price(
            store, args, entry_idx=entry_idx, exit_idx=exit_idx
        )
        entry_fills = store.load_fills_in_row_range(entry_idx, entry_idx)
        entry_umc_price_usd = recorded_entry_price(
            entry_fills, BrokerName.IBKR_UMC
        )
        entry_ccf_price, entry_ccf_source = resolve_ccf_entry_price(
            args, entry_fills=entry_fills, open_trade=open_trade
        )

        broker_pnl: dict | None = None
        realized_usd: float | None = None
        if args.from_broker:
            broker = helpers.build_umc_readonly_broker(
                config, readonly=bool(args.readonly)
            )
            broker_pnl = broker.fetch_realized_pnl()
            realized_usd = single_account_realized_usd(broker_pnl)

        settlement = build_settlement(
            config=config,
            open_trade=open_trade,
            umc_units=umc_units,
            ccf_units=ccf_units,
            ccf_contracts=ccf_contracts,
            entry_umc_price_usd=entry_umc_price_usd,
            entry_ccf_price=entry_ccf_price,
            entry_ccf_source=entry_ccf_source,
            ccf_exit_price=ccf_exit_price,
            usd_twd=usd_twd,
            realized_usd=realized_usd,
            umc_exit_price=args.umc_exit_price,
            price_tolerance_usd=float(args.price_tolerance_usd),
        )
        settlement["fx_source"] = fx_source
        settlement["ccf_exit_price_source"] = ccf_source
        settlement["broker_pnl_raw"] = broker_pnl
        print_settlement(settlement, pending=pending, state=state)
        if settlement["price_check"] is not None and not settlement["price_check"]["ok"]:
            print(
                "Refusing settle-manual-close: the supplied exit price and "
                "IBKR's realized PnL disagree by more than "
                f"{settlement['price_check']['tolerance_usd']:g} USD per share. "
                "One of them is wrong; settle only once you know which."
            )
            return 1
        if not args.apply:
            print("Dry-run only; re-run with --apply --reason <reason> to persist")
            return 0

        trade = build_settlement_trade(
            open_trade=open_trade,
            pending=pending,
            settlement=settlement,
            exit_idx=exit_idx,
            reason=str(args.reason).strip(),
        )
        store.record_trade(trade)
        state.realized_pnl += settlement["gross_pnl_twd"] - settlement["exit_fee_twd"]
        state.realized_fee_twd += settlement["exit_fee_twd"]
        state.pnl_status = "complete"
        store.save_state(
            resume_state.row_index,
            observed_at,
            state,
            resume_state.indicator,
        )
        store.record_manual_close_settlement(
            recovery_id=str(pending["recovery_id"]),
            settled_at=observed_at,
            settlement={
                **settlement,
                "reason": str(args.reason).strip(),
                "trade": trade,
                "realized_pnl_after": state.realized_pnl,
            },
        )
        store.record_event(
            resume_state.row_index,
            observed_at,
            "manual_close_settled",
            "manual-close PnL booked; pnl_status back to complete",
            {
                "recovery_id": str(pending["recovery_id"]),
                "reason": str(args.reason).strip(),
                "basis": settlement["basis"],
                "net_pnl_twd": settlement["net_pnl_twd"],
                "realized_pnl_after": state.realized_pnl,
            },
        )
        store.commit()
        print(
            "Settlement applied: pnl_status=complete, realized_pnl="
            f"{state.realized_pnl:,.2f} TWD"
        )
        return 0
    except Exception:
        store.rollback()
        raise
    finally:
        if broker is not None:
            helpers.close_brokers((broker,))
        store.close()


def resolve_settlement_fx(
    store: SQLiteStore, args: argparse.Namespace, *, created_at: str
) -> tuple[float, str]:
    """The USD/TWD rate to carry the UMC leg into the TWD ledger.

    Prefers the rate this system actually recorded around the incident over
    anything typed in afterwards -- the tick is evidence, the operator's memory
    is not.
    """
    if args.usd_twd is not None:
        rate = float(args.usd_twd)
        if rate <= 0:
            raise SystemExit("--usd-twd must be positive")
        return rate, "operator"
    observed = store.load_usd_twd_near(datetime.fromisoformat(created_at))
    if observed is None:
        raise SystemExit(
            "No recorded USD/TWD tick to settle against; pass --usd-twd with the "
            "rate at the time of the close"
        )
    return float(observed["usd_twd"]), (
        f"recorded tick {observed['observed_at']} "
        f"({observed['distance_seconds']:,.0f}s from the recovery)"
    )


def resolve_ccf_exit_price(
    store: SQLiteStore,
    args: argparse.Namespace,
    *,
    entry_idx: int,
    exit_idx: int,
) -> tuple[float, str]:
    """The CCF exit price, from the recorded fill unless overridden.

    A half-completed pair usually means the CCF leg DID fill and only the UMC
    leg was closed by hand -- that fill is in the store and is the truth.
    """
    recorded = [
        row
        for row in store.load_fills_in_row_range(entry_idx + 1, exit_idx)
        if str(row.get("broker")) == BrokerName.FUBON_CCF.value
    ]
    if args.ccf_exit_price is not None:
        supplied = float(args.ccf_exit_price)
        if recorded and abs(float(recorded[-1]["price"]) - supplied) > 1e-9:
            raise SystemExit(
                "Refusing settle-manual-close: --ccf-exit-price "
                f"{supplied:g} contradicts the recorded CCF exit fill "
                f"{float(recorded[-1]['price']):g}"
            )
        return supplied, "operator"
    if not recorded:
        raise SystemExit(
            "No recorded CCF exit fill between the entry and the recovery; pass "
            "--ccf-exit-price with the price that leg closed at"
        )
    last = recorded[-1]
    return float(last["price"]), f"recorded fill {last['fill_id']}"


def recorded_entry_price(entry_fills: list[dict], broker: BrokerName) -> float | None:
    for row in entry_fills:
        if str(row.get("broker")) == broker.value:
            return float(row["price"])
    return None


def resolve_ccf_entry_price(
    args: argparse.Namespace, *, entry_fills: list[dict], open_trade: dict
) -> tuple[float, str]:
    """What the CCF leg actually cost to open.

    The strategy books an entry at the BAR's ccf close and then fills at
    whatever the market gave, so open_trade['entry_ccf_close'] and the recorded
    fill can differ -- on 2026-08-18 by half a point, which is 500 TWD on one
    lot. A settlement is a record of money that moved, so the fill wins here
    and the booked figure is reported beside it rather than silently used.
    """
    booked = float(open_trade["entry_ccf_close"])
    if args.ccf_entry_price is not None:
        return float(args.ccf_entry_price), "operator"
    recorded = recorded_entry_price(entry_fills, BrokerName.FUBON_CCF)
    if recorded is None:
        return booked, "strategy entry (no recorded fill)"
    return recorded, "recorded entry fill"


def single_account_realized_usd(payload: dict) -> float:
    """Realized PnL for the one account, refusing anything ambiguous."""
    rows = list(payload.get("pnl") or ())
    if len(rows) != 1:
        raise SystemExit(
            "Refusing settle-manual-close: IBKR reported "
            f"{len(rows)} accounts; this command settles a single-account "
            "session only"
        )
    realized = rows[0].get("realized_pnl_usd")
    if realized is None:
        raise SystemExit(
            "IBKR did not report a realized PnL before the deadline; the PnL "
            "subscription never updated"
        )
    ledger = dict(payload.get("ledger_realized") or {})
    ledger_usd = ledger.get("USD")
    # Two independent reads of one number. Disagreement means something is
    # being misread, and a settlement is not the place to guess which.
    if ledger_usd is not None and abs(float(ledger_usd) - float(realized)) > 1.0:
        raise SystemExit(
            "Refusing settle-manual-close: IBKR's PnL subscription "
            f"({float(realized):,.2f}) and account ledger ({float(ledger_usd):,.2f}) "
            "disagree on realized PnL"
        )
    return float(realized)


def build_settlement(
    *,
    config: object,
    open_trade: dict,
    umc_units: float,
    ccf_units: float,
    ccf_contracts: int,
    entry_umc_price_usd: float | None,
    entry_ccf_price: float,
    entry_ccf_source: str,
    ccf_exit_price: float,
    usd_twd: float,
    realized_usd: float | None,
    umc_exit_price: float | None,
    price_tolerance_usd: float,
) -> dict:
    """Price the round trip, on the broker's number where one is available."""
    fees = config.fees
    entry_umc_twd_fair = float(open_trade["entry_umc_twd_fair"])
    entry_umc_fee_twd = float(open_trade["entry_umc_fee_twd"])
    multiplier = float(fees.umc_contract_multiplier)
    # The order that CLOSES the position is the opposite of the one that opened
    # it, and IBKR charges regulatory fees to the seller alone -- the same
    # negation _fill_exit applies before pricing an exit.
    exit_side = OrderSide.BUY if -umc_units > 0 else OrderSide.SELL

    def costs_at(exit_umc_twd_fair: float) -> dict[str, float]:
        return fill_costs(
            umc_units=umc_units,
            umc_price=exit_umc_twd_fair,
            ccf_contracts=ccf_contracts,
            ccf_price=ccf_exit_price,
            fees=fees,
            umc_side=exit_side,
            usd_twd_rate=usd_twd,
        )

    implied_exit_price: float | None = None
    if realized_usd is not None and entry_umc_price_usd is not None:
        # Solve R = umc_units x (exit - entry) - commissions for `exit`. The
        # exit commission depends on the exit price only through a
        # 1%-of-value cap that never binds at these sizes, so the loop
        # converges on the first pass; it runs twice to prove it rather than
        # to assume it.
        entry_fee_usd = entry_umc_fee_twd / usd_twd
        guess_twd_fair = entry_umc_twd_fair
        for _ in range(2):
            exit_fee_usd = costs_at(guess_twd_fair)["umc_fee_twd"] / usd_twd
            implied_exit_price = entry_umc_price_usd + (
                (realized_usd + entry_fee_usd + exit_fee_usd) / umc_units
            )
            guess_twd_fair = implied_exit_price * usd_twd / multiplier

    settled_exit_price = (
        float(umc_exit_price) if umc_exit_price is not None else implied_exit_price
    )
    if settled_exit_price is None:
        raise SystemExit(
            "Could not establish a UMC exit price: IBKR's realized PnL needs the "
            "recorded entry fill to solve against, and this store has none. Pass "
            "--umc-exit-price."
        )
    exit_umc_twd_fair = settled_exit_price * usd_twd / multiplier
    exit_costs = costs_at(exit_umc_twd_fair)

    price_check = None
    if implied_exit_price is not None and umc_exit_price is not None:
        gap = abs(float(umc_exit_price) - implied_exit_price)
        price_check = {
            "supplied_usd": float(umc_exit_price),
            "implied_usd": implied_exit_price,
            "gap_usd": gap,
            "tolerance_usd": price_tolerance_usd,
            "ok": gap <= price_tolerance_usd,
        }

    ccf_pnl = ccf_units * (ccf_exit_price - entry_ccf_price)
    model_umc_pnl = umc_units * (
        umc_contract_twd_price(exit_umc_twd_fair, fees)
        - umc_contract_twd_price(entry_umc_twd_fair, fees)
    )
    if realized_usd is not None:
        basis = "broker_realized"
        umc_leg_net_twd = realized_usd * usd_twd
        # IBKR's figure is already net of BOTH commissions, while the entry fee
        # was deducted from realized_pnl back at entry. Adding both fees into
        # the gross and subtracting them again below leaves the leg's NET
        # exactly equal to the broker's number; only the gross/fee split uses
        # the model, and that split cancels.
        umc_pnl = umc_leg_net_twd + entry_umc_fee_twd + exit_costs["umc_fee_twd"]
    else:
        basis = "model"
        umc_pnl = model_umc_pnl
        umc_leg_net_twd = umc_pnl - entry_umc_fee_twd - exit_costs["umc_fee_twd"]

    gross_pnl_twd = umc_pnl + ccf_pnl
    entry_fee_twd = float(open_trade["entry_fee_twd"])
    total_fee_twd = entry_fee_twd + exit_costs["total_fee_twd"]
    return {
        "basis": basis,
        "usd_twd": usd_twd,
        "realized_usd": realized_usd,
        "umc_exit_price_usd": settled_exit_price,
        "umc_exit_price_implied_usd": implied_exit_price,
        "umc_exit_price_source": (
            "operator" if umc_exit_price is not None else "implied from IBKR realized PnL"
        ),
        "entry_umc_price_usd": entry_umc_price_usd,
        "entry_umc_twd_fair": entry_umc_twd_fair,
        "exit_umc_twd_fair": exit_umc_twd_fair,
        "entry_ccf_price": entry_ccf_price,
        "entry_ccf_source": entry_ccf_source,
        "entry_ccf_close_booked": float(open_trade["entry_ccf_close"]),
        "ccf_exit_price": ccf_exit_price,
        "price_check": price_check,
        "umc_pnl": umc_pnl,
        "umc_pnl_model_basis": model_umc_pnl,
        "umc_leg_net_twd": umc_leg_net_twd,
        "ccf_pnl": ccf_pnl,
        "ccf_pnl_bar_basis": ccf_units
        * (ccf_exit_price - float(open_trade["entry_ccf_close"])),
        "gross_pnl_twd": gross_pnl_twd,
        "exit_umc_fee_twd": exit_costs["umc_fee_twd"],
        "exit_ccf_fee_twd": exit_costs["ccf_fee_twd"],
        "exit_ccf_tax_twd": exit_costs["ccf_tax_twd"],
        "exit_fee_twd": exit_costs["total_fee_twd"],
        "entry_fee_twd": entry_fee_twd,
        "total_fee_twd": total_fee_twd,
        "net_pnl_twd": gross_pnl_twd - total_fee_twd,
    }


def print_settlement(settlement: dict, *, pending: dict, state: object) -> None:
    print(
        f"Manual close {pending['recovery_id']} "
        f"(recorded {pending['created_at']})"
    )
    print(f"  basis            : {settlement['basis']}")
    print(
        f"  usd_twd          : {settlement['usd_twd']:.5f}  "
        f"[{settlement['fx_source']}]"
    )
    if settlement["realized_usd"] is not None:
        print(
            f"  IBKR realized    : {settlement['realized_usd']:+,.2f} USD "
            "(net of commissions)"
        )
    print(
        f"  UMC exit price   : {settlement['umc_exit_price_usd']:.5f} USD  "
        f"[{settlement['umc_exit_price_source']}]"
    )
    check = settlement["price_check"]
    if check is not None:
        verdict = "ok" if check["ok"] else "DISAGREES"
        print(
            f"  price cross-check: supplied {check['supplied_usd']:.5f} vs "
            f"implied {check['implied_usd']:.5f} -> "
            f"{check['gap_usd']:.5f} USD {verdict}"
        )
    print(
        f"  CCF entry price  : {settlement['entry_ccf_price']:g}  "
        f"[{settlement['entry_ccf_source']}]"
    )
    booked = settlement["entry_ccf_close_booked"]
    if abs(booked - settlement["entry_ccf_price"]) > 1e-9:
        print(
            f"    NOTE the strategy booked its entry at the bar price {booked:g}, "
            f"not the fill. Settling on the fill changes the CCF leg by "
            f"{settlement['ccf_pnl'] - settlement['ccf_pnl_bar_basis']:+,.2f} TWD."
        )
    print(
        f"  CCF exit price   : {settlement['ccf_exit_price']:g}  "
        f"[{settlement['ccf_exit_price_source']}]"
    )
    print(
        f"  UMC leg          : gross {settlement['umc_pnl']:+,.2f}  "
        f"net {settlement['umc_leg_net_twd']:+,.2f} TWD"
    )
    if settlement["basis"] == "broker_realized":
        print(
            f"    the TWD-marked model would book "
            f"{settlement['umc_pnl_model_basis']:+,.2f} gross; the gap is the FX "
            "move on the notional, which the USD proceeds already offset"
        )
    print(f"  CCF leg          : gross {settlement['ccf_pnl']:+,.2f} TWD")
    print(
        f"  fees             : entry {settlement['entry_fee_twd']:,.2f} + exit "
        f"{settlement['exit_fee_twd']:,.2f} = {settlement['total_fee_twd']:,.2f} TWD"
    )
    print(f"  NET              : {settlement['net_pnl_twd']:+,.2f} TWD")
    print(
        f"  realized_pnl     : {state.realized_pnl:,.2f} -> "
        f"{state.realized_pnl + settlement['gross_pnl_twd'] - settlement['exit_fee_twd']:,.2f} TWD"
    )


def build_settlement_trade(
    *,
    open_trade: dict,
    pending: dict,
    settlement: dict,
    exit_idx: int,
    reason: str,
) -> dict:
    """A trades row for the settled round trip.

    Without one the equity series moves and the trade list does not, which is
    the sort of quiet disagreement between two views of the same money that
    this store is otherwise careful to prevent.
    """
    original = pending["original_state"]
    exit_time = datetime.fromisoformat(pending["created_at"])
    entry_time = open_trade["entry_time"]
    if isinstance(entry_time, str):
        entry_time = datetime.fromisoformat(entry_time)
    return {
        **open_trade,
        "entry_ccf_close": settlement["entry_ccf_price"],
        # trades.exit_signal_{idx,time} are NOT NULL, and a position closed by
        # hand need never have had an exit signal at all -- the operator can
        # simply have decided. -1 is the state's own "no signal" sentinel.
        # Falling back to the recovery itself keeps that case from failing at
        # the INSERT, after every check has already passed.
        "exit_signal_idx": (
            exit_idx
            if int(original.get("exit_signal_idx") or -1) < 0
            else int(original["exit_signal_idx"])
        ),
        "exit_signal_time": original.get("exit_signal_time") or exit_time,
        "exit_signal_zscore": original.get("exit_signal_zscore"),
        "exit_idx": exit_idx,
        "exit_time": exit_time,
        # No bar priced this exit -- it happened outside the loop -- and
        # inventing a z-score for it would read as though one had.
        "exit_fill_zscore": None,
        "exit_umc_twd_fair": settlement["exit_umc_twd_fair"],
        "exit_ccf_close": settlement["ccf_exit_price"],
        "exit_fill_price_type": "manual_settlement",
        "umc_pnl": settlement["umc_pnl"],
        "ccf_pnl": settlement["ccf_pnl"],
        "gross_pnl_twd": settlement["gross_pnl_twd"],
        "exit_umc_fee_twd": settlement["exit_umc_fee_twd"],
        "exit_ccf_fee_twd": settlement["exit_ccf_fee_twd"],
        "exit_ccf_tax_twd": settlement["exit_ccf_tax_twd"],
        "exit_fee_twd": settlement["exit_fee_twd"],
        "umc_fee_twd": float(open_trade["entry_umc_fee_twd"])
        + settlement["exit_umc_fee_twd"],
        "ccf_fee_twd": float(open_trade["entry_ccf_fee_twd"])
        + settlement["exit_ccf_fee_twd"],
        "ccf_tax_twd": float(open_trade["entry_ccf_tax_twd"])
        + settlement["exit_ccf_tax_twd"],
        "total_fee_twd": settlement["total_fee_twd"],
        "net_pnl_twd": settlement["net_pnl_twd"],
        "total_pnl": settlement["net_pnl_twd"],
        "exit_reason": "manual_close_settlement",
        "holding_minutes": minutes_between(entry_time, exit_time),
        "settlement_basis": settlement["basis"],
        "settlement_reason": reason,
    }


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


__all__ = ["command_recover_manual_flat", "command_settle_manual_close"]
