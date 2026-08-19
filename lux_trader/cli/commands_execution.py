"""Real-execution CLI commands: live-execute, exec-smoke, manual-close, broker-status.

Everything here either sends REAL orders behind explicit config+env gates or
reads real accounts read-only. Tests monkeypatch the adapter names on this
module (e.g. ``commands_execution.FubonFutureExecutionAdapter``).

``manual-close`` and ``exec-smoke`` both accept ``fubon`` and ``ibkr``. The IBKR
half of manual-close was missing until 2026-08-04, which meant a stranded UMC leg
had no emergency-close path from this CLI at all -- a gap in the safety net, not
merely a testing inconvenience.

The IBKR execution surface is imported LAZILY (see ``_load_ibkr_execution``).
A module-scope import reaches this module through ``dispatch.py`` on every CLI
invocation, including ``replay`` and ``summary``, which must keep working with no
brokerage packages installed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from typing import Any

from lux_trader.crash_log import record_crash

from lux_trader.cli import helpers
from lux_trader.config import load_config
from lux_trader.core.models import BrokerName, Direction, OrderSide
from lux_trader.execution.gate import (
    assert_live_execution_gate_open,
    evaluate_live_execution_gate,
)
from lux_trader.execution.intent import (
    ExecutionLeg,
    ExecutionPlanType,
    PairExecutionPlan,
)
from lux_trader.integrations.fubon.execution import (
    FUBON_EXECUTION_SMOKE_ENV_GATES,
    FUBON_MANUAL_CLOSE_ENV_GATES,
    FubonFutureExecutionAdapter,
    fubon_manual_close_env_gates_open,
    fubon_smoke_env_gates_open,
)
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.integrations.fubon.execution_process import FubonFutureExecutionProcess
from lux_trader.integrations.venues import open_umc_readonly_broker
from lux_trader.reconciliation import ReadOnlyBroker
from lux_trader.runtime.live import LiveExecuteRunner
from lux_trader.runtime.live.lease import (
    LiveProcessLease,
    assert_live_lease_available,
)
from lux_trader.store import SQLiteStore


# ---------------------------------------------------------------------------
# live-execute + doctor --mode order
# ---------------------------------------------------------------------------


def build_live_execution_gate_report(config: object, store: SQLiteStore):
    return evaluate_live_execution_gate(
        config,
        reconciliation_report=store.load_latest_reconciliation_report(),
        now=datetime.now().astimezone(),
        include_plan_checks=False,
    )


def run_order_doctor_checks(config: object) -> tuple[bool, list[str]]:
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        report = build_live_execution_gate_report(config, store)
    finally:
        store.close()

    lines = [
        f"store_path={config.store_path}",
        "execution=real_two_leg_coordinator (ccf_first)",
    ]
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"{status} {check.check_type}: {check.message}")
    return report.passed, lines


def command_live_execute(args: argparse.Namespace) -> int:
    from lux_trader.cli.commands_live import (
        build_live_reporter,
        reconcile_brokers_to_store,
    )

    config = load_config(args.config)
    reporter = build_live_reporter(args, config, mode="live-execute")
    lease = LiveProcessLease(config.store_path)
    shared_fubon: FubonFutureExecutionProcess | None = None
    shared_brokers: tuple[ReadOnlyBroker, ...] | None = None
    try:
        lease.acquire(metadata={"mode": "live-execute"})
        store = SQLiteStore(config.store_path)
        try:
            if args.reset_store:
                store.reset()
            store.initialize()
            resume_state = store.load_resume_state()
            ccf_symbol = helpers.reconciliation_ccf_symbol(
                config,
                resume_state.strategy if resume_state is not None else None,
            )
            shared_fubon, shared_brokers = build_live_execution_brokers(
                config,
                ccf_symbol,
            )
            if config.live_execution.require_readonly_reconciliation:
                run_id, reconciliation_report = reconcile_brokers_to_store(
                    config,
                    store,
                    readonly=True,
                    brokers=shared_brokers,
                )
                reporter.event(
                    datetime.now().astimezone(),
                    "startup",
                    "readonly_reconciliation "
                    f"run_id={run_id} status={reconciliation_report.status.value}",
                )
            else:
                reconciliation_report = store.load_latest_reconciliation_report()
            gate_report = evaluate_live_execution_gate(
                config,
                reconciliation_report=reconciliation_report,
                include_plan_checks=False,
            )
        finally:
            store.close()
        assert_live_execution_gate_open(gate_report)
        LiveExecuteRunner(
            config,
            reporter=reporter,
            fubon_adapter=shared_fubon,
            readonly_brokers=shared_brokers,
        ).run(
            resume=args.resume,
            # A requested reset must happen before reconciliation so the fresh
            # report remains in the same store used by the live runtime.
            reset_store=False,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except Exception as exc:
        # Write the stack BEFORE reporting: the SystemExit below prints only the
        # message, so without this the traceback exists nowhere once the
        # terminal scrolls. See lux_trader/crash_log.py.
        crash_path = record_crash(exc, context="live-execute")
        reporter.error(
            datetime.now().astimezone(),
            f"{type(exc).__name__}: {exc}"
            + (f" [traceback: {crash_path}]" if crash_path else ""),
        )
        if isinstance(exc, RuntimeError):
            raise SystemExit(
                f"{exc}"
                + (f"\nFull traceback written to {crash_path}" if crash_path else "")
            )
        raise
    finally:
        helpers.close_brokers(shared_brokers or ())
        lease.release()
        reporter.finish()
    return 0


def build_live_execution_brokers(
    config: object,
    ccf_symbol: str,
) -> tuple[
    FubonFutureExecutionProcess | None,
    tuple[ReadOnlyBroker, ...] | None,
]:
    if ccf_symbol.strip().lower() == "auto":
        return None, None
    fubon = FubonFutureExecutionProcess(
        ccf_symbol,
        config.live.fubon_env_path,
    )
    return fubon, (
        fubon,
        open_umc_readonly_broker(
            config.live.umc_symbol,
            config.live.fubon_env_path,
            config,
        ),
    )


# ---------------------------------------------------------------------------
# exec-smoke --venue {fubon}
# ---------------------------------------------------------------------------


IBKR_EXECUTION_SMOKE_ENV = "LUX_IBKR_EXECUTION_SMOKE"

# Assigned lazily by _load_ibkr_execution(). Importing this at module scope
# would drag ib_async into EVERY cli invocation through dispatch.py -- including
# `replay` and `summary`, which venues.py:19-22 requires to keep working on a
# machine with no brokerage packages installed. ib_async is not in any
# requirements file, so a module-scope import breaks a clean checkout outright.
# The name stays module-level so tests can monkeypatch it.
IbkrUmcExecutionAdapter: Any = None


def _load_ibkr_execution() -> None:
    """Import the IBKR execution surface on first use, not at import time."""
    global IbkrUmcExecutionAdapter
    if IbkrUmcExecutionAdapter is None:
        from lux_trader.integrations.ibkr.execution import (
            IbkrUmcExecutionAdapter as _Adapter,
        )

        IbkrUmcExecutionAdapter = _Adapter


def ibkr_env_gate_names() -> tuple[str, ...]:
    from lux_trader.integrations.ibkr.execution import IBKR_LIVE_ORDER_ENV_GATES

    return IBKR_LIVE_ORDER_ENV_GATES


def ibkr_env_gates_open() -> dict[str, bool]:
    from lux_trader.integrations.ibkr.execution import ibkr_live_order_gates_open

    return ibkr_live_order_gates_open()


def refuse_foreign_venue_args(
    args: argparse.Namespace,
    *,
    venue: str,
    foreign: tuple[tuple[str, str], ...],
) -> None:
    """Reject args that belong to a different venue rather than ignoring them.

    argparse cannot do this: the options live on one shared subparser, so
    widening `--venue` silently turned previously-rejected flags into accepted
    no-ops. `exec-smoke --venue fubon --side sell` used to die with
    "unrecognized arguments"; without this it parses and opens a real LONG,
    because the Fubon path hard-codes BUY and never reads `--side`.
    """
    supplied = [
        flag
        for attr, flag in foreign
        if getattr(args, attr, None) not in (None, False)
    ]
    if supplied:
        raise SystemExit(
            f"{', '.join(supplied)} {'is' if len(supplied) == 1 else 'are'} "
            f"not supported for --venue {venue} and would be ignored"
        )


def command_exec_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    if args.venue == "ibkr":
        refuse_foreign_venue_args(
            args, venue="ibkr", foreign=(("lot", "--lot"),)
        )
        return ibkr_exec_smoke(args)
    # --shares carries a default of 1, so it cannot be distinguished from an
    # explicit 1; --side likewise defaults to "buy". Only flag the values a
    # Fubon operator could not have meant.
    refuse_foreign_venue_args(
        args,
        venue="fubon",
        foreign=(("allow_position_mismatch", "--allow-position-mismatch"),),
    )
    if str(getattr(args, "side", "buy")).lower() != "buy":
        raise SystemExit(
            "--side is not supported for --venue fubon (the smoke round trip "
            "always opens with a buy) and would be ignored"
        )
    if args.symbol is None:
        raise SystemExit("--symbol is required for --venue fubon")
    if args.lot is None:
        raise SystemExit("--lot is required for --venue fubon")
    return fubon_exec_smoke(args)


def ibkr_exec_smoke(args: argparse.Namespace) -> int:
    """Open and immediately close a tiny real UMC position.

    This proves the ORDER PATH: placement, the three-tier fill confirmation,
    whole-share rounding, and that the position actually moves. It proves
    nothing about the pair. Two gaps are worth naming rather than discovering:

      It builds its own plan, so it never touches strategy -> price_policy ->
      validator. That is the path that rejected every entry on 2026-08-04
      while a full test suite stayed green.

      It is one leg at one venue, so it cannot produce the state the pair
      actually fears -- CCF filled, UMC unknown. A clean round trip here is the
      happy path only.
    """
    config = load_config(args.config)
    require_ibkr_exec_smoke_ready(config, args)

    symbol = str(config.live.umc_symbol).strip().upper()
    shares = int(args.shares)
    open_side = OrderSide.BUY if str(args.side).lower() == "buy" else OrderSide.SELL
    close_side = OrderSide.SELL if open_side == OrderSide.BUY else OrderSide.BUY
    _load_ibkr_execution()
    adapter = IbkrUmcExecutionAdapter(
        symbol,
        host=config.live.ibkr_host,
        port=config.live.ibkr_port,
    )
    try:
        preflight = adapter.preflight()
        if preflight.open_orders:
            raise SystemExit(
                "Refusing IBKR execution smoke with existing open orders: "
                f"{len(preflight.open_orders)}"
            )
        if abs(preflight.position_quantity) > 1e-12:
            raise SystemExit(
                "Refusing IBKR execution smoke with nonzero position: "
                f"{preflight.position_quantity:g}. A round trip from a nonzero "
                "start cannot tell its own fills from what was already there."
            )
        print(
            f"IBKR execution smoke preflight passed: symbol={symbol}, "
            f"shares={shares}, open={open_side.value}, close={close_side.value}"
        )

        open_outcome = adapter.execute(
            build_ibkr_smoke_plan(
                symbol=symbol,
                shares=shares,
                side=open_side,
                plan_type=ExecutionPlanType.ENTRY,
                timestamp=datetime.now().astimezone().replace(microsecond=0),
            )
        )
        print_ibkr_smoke_outcome("open", open_outcome)
        # round, not int: truncating a 2.5-share fill to 2 closes short and
        # strands the remainder, which then trips the CRITICAL check below.
        opened = int(round(sum(fill.quantity for fill in open_outcome.fills)))
        if opened <= 0:
            # Guarded: this runs precisely when the venue is unhealthy, so the
            # read is the thing most likely to raise -- and losing it would
            # swallow the CRITICAL banner and the flatten command with a real
            # position possibly open.
            position = read_position_or_report(adapter, "open_unknown_diagnostic")
            print("CRITICAL manual intervention required")
            print_ibkr_manual_close_hint(symbol, position)
            return 1

        after_open = fetch_position_with_retry(adapter, expected="nonzero")
        print(f"IBKR execution smoke after_open: position={after_open:g}")
        if abs(after_open) <= 1e-12:
            print(
                "WARN IBKR position query returned zero after a reported fill; "
                "continuing with the close because the fill was reported"
            )

        # Close the quantity that actually FILLED, not the quantity requested.
        # A partial open closed at the requested size would flip the position.
        close_outcome = adapter.execute(
            build_ibkr_smoke_plan(
                symbol=symbol,
                shares=opened,
                side=close_side,
                plan_type=ExecutionPlanType.EXIT,
                timestamp=datetime.now().astimezone().replace(microsecond=0),
            )
        )
        print_ibkr_smoke_outcome("close", close_outcome)

        # Guarded for the same reason as the unknown-open path: a close order
        # has already been sent, so a read failure here must not replace the
        # CRITICAL banner with a traceback.
        try:
            final_position = fetch_position_with_retry(adapter, expected="zero")
            final_open_orders = adapter.fetch_open_orders()
        except Exception as exc:  # noqa: BLE001 - report, never mask
            print(
                f"IBKR execution smoke after_close: POSITION UNREADABLE "
                f"({type(exc).__name__}: {exc}) after a close order was sent."
            )
            print("CRITICAL manual intervention required")
            print_ibkr_manual_close_hint(symbol, None)
            return 1
        print(
            f"IBKR execution smoke after_close: position={final_position:g}, "
            f"open_orders={len(final_open_orders)}"
        )
        if (
            not open_outcome.filled
            or not close_outcome.filled
            or final_open_orders
            or abs(final_position) > 1e-12
        ):
            print("CRITICAL manual intervention required")
            print_ibkr_manual_close_hint(symbol, final_position)
            return 1

        print(
            f"IBKR execution smoke complete: position={final_position:g}, "
            f"open_orders={len(final_open_orders)}. Read the real commission "
            "off IBKR's own statement -- integrations/ibkr/fees.py has never "
            "been checked against a bill."
        )
        return 0
    finally:
        adapter.close()


def print_ibkr_smoke_outcome(label: str, outcome: object) -> None:
    filled = sum(fill.quantity for fill in getattr(outcome, "fills", ()))
    print(
        f"IBKR execution smoke {label}: status={outcome.status.value}, "
        f"fills={len(outcome.fills)}, filled_shares={filled:g}, "
        f"message={outcome.message}"
    )


def read_position_or_report(adapter: object, label: str) -> float | None:
    """Read the position, or say so and return None rather than raising.

    Every caller is on a path where an order may already be live, so an
    exception here would replace the CRITICAL banner and the flatten command
    with a traceback -- at the one moment the operator needs both. Returns None
    for "unreadable", which is NOT the same as flat.
    """
    try:
        preflight = adapter.preflight()
    except Exception as exc:  # noqa: BLE001 - report, never mask
        print(
            f"IBKR {label}: POSITION UNREADABLE ({type(exc).__name__}: {exc}). "
            "Treat this as a possible open position, not a flat one."
        )
        return None
    print(
        f"IBKR {label}: position={preflight.position_quantity:g}, "
        f"open_orders={len(preflight.open_orders)}"
    )
    return float(preflight.position_quantity)


def print_ibkr_manual_close_hint(symbol: str, position: float | None) -> None:
    """The remedy, spelled out, because this is read during an incident."""
    if position is None:
        print(
            "Cannot print a flatten command without a readable position. "
            "Check IBKR directly, then run: admin manual-close --venue ibkr "
            f"--symbol {symbol} --side <buy|sell> --shares <n> "
            f"--confirm-symbol {symbol}"
        )
        return
    # round, not int: a residual of 2.9999999996 must print 3, and int() would
    # print 2 and strand a share. A residual under half a share rounds to 0,
    # which --shares rejects, so say that instead of emitting a dead command.
    shares = int(round(abs(position)))
    if shares <= 0:
        print(
            f"Position {position:g} rounds to zero whole shares; nothing to "
            "flatten, but verify against IBKR directly."
        )
        return
    side = "sell" if position > 0 else "buy"
    print(
        "To flatten: admin manual-close --venue ibkr "
        f"--symbol {symbol} --side {side} --shares {shares} "
        f"--confirm-symbol {symbol}"
    )


def require_ibkr_symbol(config: object, args: argparse.Namespace) -> str:
    """The instrument this command will actually trade, or refuse.

    IbkrClientProcess takes no symbol: `_resolve_umc_detail` hardcodes
    Stock("UMC", "SMART", "USD") and `fetch_umc_position` filters on "UMC". So
    an unchecked `--symbol` is not merely ignored -- it is a lie the journal
    then records. Confirming `--symbol` against `--confirm-symbol` alone
    catches nothing, because a wrong instrument typed twice still matches
    itself; both must agree with the configured UMC symbol.
    """
    configured = str(config.live.umc_symbol).strip().upper()
    supplied = str(getattr(args, "symbol", None) or configured).strip().upper()
    confirm = str(args.confirm_symbol).strip().upper()
    if supplied != configured:
        raise SystemExit(
            f"--symbol {supplied} is not the configured UMC symbol "
            f"({configured}). This command can only trade {configured}: the "
            "IBKR worker resolves that contract regardless of what is passed."
        )
    if confirm != configured:
        raise SystemExit(
            f"--confirm-symbol must match {configured}; got {confirm}"
        )
    return configured


def require_ibkr_exec_smoke_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    require_ibkr_symbol(config, args)
    if int(args.shares) <= 0:
        raise SystemExit("--shares must be positive")
    # A third gate on top of the adapter's two. Enabling live orders for the
    # trading loop must not also enable a tool whose whole purpose is to open a
    # position for no trading reason. manual-close deliberately does NOT carry
    # an extra gate -- it is the emergency exit, and an exit should not be
    # harder to reach than an entrance.
    if os.getenv(IBKR_EXECUTION_SMOKE_ENV, "").strip() != "1":
        raise SystemExit(
            f"IBKR execution smoke gates closed: {IBKR_EXECUTION_SMOKE_ENV}=1"
        )
    gates = ibkr_env_gates_open()
    closed = [name for name in ibkr_env_gate_names() if not gates[name]]
    if closed:
        raise SystemExit(
            "IBKR execution smoke gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def build_ibkr_smoke_plan(
    *,
    symbol: str,
    shares: int,
    side: OrderSide,
    plan_type: ExecutionPlanType,
    timestamp: datetime,
) -> PairExecutionPlan:
    direction = (
        Direction.LONG_UMC_SHORT_CCF
        if (side == OrderSide.BUY) == (plan_type == ExecutionPlanType.ENTRY)
        else Direction.SHORT_UMC_LONG_CCF
    )
    return PairExecutionPlan(
        plan_id=(
            f"IBKR-SMOKE-{timestamp.strftime('%Y%m%d%H%M%S')}-{plan_type.value}"
        ),
        plan_type=plan_type,
        direction=direction,
        timestamp=timestamp,
        row_index=-1,
        legs=(
            ExecutionLeg(
                broker=BrokerName.IBKR_UMC,
                symbol=symbol,
                side=side,
                quantity=float(shares),
                price=1.0,
                timestamp=timestamp,
                row_index=-1,
            ),
        ),
        reason="ibkr_execution_smoke",
        decision_spread_type="manual_smoke",
    )


def fubon_exec_smoke(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_fubon_exec_smoke_ready(config, args)

    symbol = str(args.symbol).strip()
    lot = int(args.lot)
    adapter = FubonFutureExecutionAdapter(
        symbol,
        config.live.fubon_env_path,
    )
    try:
        preflight = adapter.preflight()
        if preflight.open_orders:
            raise SystemExit(
                "Refusing Fubon execution smoke with existing open orders: "
                f"{len(preflight.open_orders)}"
            )
        if abs(preflight.position_quantity) > 1e-12:
            raise SystemExit(
                "Refusing Fubon execution smoke with nonzero position: "
                f"{preflight.position_quantity}"
            )

        timestamp = datetime.now().astimezone().replace(microsecond=0)
        print(
            "Fubon execution smoke preflight passed: "
            f"symbol={symbol}, lot={lot}"
        )

        entry_plan = build_fubon_smoke_plan(
            symbol=symbol,
            lot=lot,
            side=OrderSide.BUY,
            plan_type=ExecutionPlanType.ENTRY,
            timestamp=timestamp,
        )
        entry_outcome = adapter.execute(entry_plan)
        print_fubon_smoke_outcome("entry", entry_outcome)
        entry_filled_lot = int(sum(fill.quantity for fill in entry_outcome.fills))
        if entry_filled_lot <= 0:
            diagnostic_position = adapter.fetch_position_quantity()
            diagnostic_open_orders = adapter.fetch_open_orders()
            print_fubon_smoke_position(
                "entry_unknown_diagnostic",
                position=diagnostic_position,
                open_orders=len(diagnostic_open_orders),
            )
            print_fubon_smoke_order_records(
                adapter.fetch_order_records(),
                raw_json=bool(args.raw_json),
            )
            print("CRITICAL manual intervention required")
            return 1
        after_entry_position = fetch_fubon_position_with_retry(
            adapter,
            expected="nonzero",
        )
        after_entry_open_orders = adapter.fetch_open_orders()
        print_fubon_smoke_position(
            "after_entry",
            position=after_entry_position,
            open_orders=len(after_entry_open_orders),
        )
        if abs(after_entry_position) <= 1e-12:
            print(
                "WARN Fubon position query returned zero after a filled entry; "
                "continuing with close attempt because entry fill was reported"
            )

        exit_plan = build_fubon_smoke_plan(
            symbol=symbol,
            lot=entry_filled_lot,
            side=OrderSide.SELL,
            plan_type=ExecutionPlanType.EXIT,
            timestamp=datetime.now().astimezone().replace(microsecond=0),
        )
        exit_outcome = adapter.execute(exit_plan)
        print_fubon_smoke_outcome("exit", exit_outcome)

        final_open_orders = adapter.fetch_open_orders()
        final_position = fetch_fubon_position_with_retry(
            adapter,
            expected="zero",
        )
        print_fubon_smoke_position(
            "after_exit",
            position=final_position,
            open_orders=len(final_open_orders),
        )
        print_fubon_smoke_order_records(
            adapter.fetch_order_records(),
            raw_json=bool(args.raw_json),
        )
        if (
            not entry_outcome.filled
            or not exit_outcome.filled
            or final_open_orders
            or abs(final_position) > 1e-12
        ):
            print("CRITICAL manual intervention required")
            print(
                "Fubon execution smoke final state: "
                f"position={final_position}, open_orders={len(final_open_orders)}"
            )
            return 1

        print(
            "Fubon execution smoke complete: "
            f"position={final_position}, open_orders={len(final_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# manual-close --venue {fubon, ibkr}
# ---------------------------------------------------------------------------


def command_manual_close(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    # The lease exists for Fubon's one-account-one-SDK-session limit, which IBKR
    # does not share -- the execution adapter takes its own client id. It is
    # applied to both anyway for a second reason: racing a running live-execute
    # loop's own position management is a hazard at either venue. A dry-run does
    # not take the lease, so a soak never blocks an emergency close.
    assert_live_lease_available(config.store_path)
    if args.venue == "ibkr":
        refuse_foreign_venue_args(
            args, venue="ibkr", foreign=(("lot", "--lot"),)
        )
        return ibkr_manual_close(args)
    # --allow-position-mismatch is accepted for Fubon now that Fubon actually
    # has a close-only check to override. Blocking it while no check existed
    # guarded an escape hatch for a door that was standing open.
    refuse_foreign_venue_args(
        args,
        venue="fubon",
        foreign=(("shares", "--shares"),),
    )
    if args.lot is None:
        raise SystemExit("--lot is required for --venue fubon")
    return fubon_manual_close(args)


def side_closes_position(position: float, side: OrderSide) -> bool:
    """Whether this side reduces the position rather than growing or flipping it."""
    if abs(position) <= 1e-12:
        return False
    return (position > 0) == (side == OrderSide.SELL)


def ibkr_manual_close(args: argparse.Namespace) -> int:
    """Emergency-close a stranded UMC leg.

    This is the only tool that sends a UMC order outside the pair runtime, and
    it exists for the state where the CCF leg filled and the UMC leg did not --
    a naked leg. So it is deliberately close-only: the failure it must never
    cause is turning a stranded long into a naked short, which would look like
    a successful close in the log and double the exposure in the account.
    """
    config = load_config(args.config)
    require_ibkr_manual_close_ready(config, args)

    symbol = str(args.symbol).strip().upper()
    shares = int(args.shares)
    side = OrderSide.BUY if str(args.side).lower() == "buy" else OrderSide.SELL
    _load_ibkr_execution()
    adapter = IbkrUmcExecutionAdapter(
        symbol,
        host=config.live.ibkr_host,
        port=config.live.ibkr_port,
    )
    try:
        # An unreadable position raises out of here rather than being treated as
        # flat -- Phase D invariant 1. Refusing costs a missed close; guessing
        # costs a doubled position.
        preflight = adapter.preflight()
        pre_position = preflight.position_quantity
        pre_open_orders = preflight.open_orders
        print(
            f"IBKR manual close precheck: position={pre_position:g}, "
            f"open_orders={len(pre_open_orders)}"
        )
        if pre_open_orders:
            raise SystemExit(
                "Refusing IBKR manual close with existing open orders: "
                f"{len(pre_open_orders)}. Cancel them first, or they will fill "
                "against this one."
            )

        mismatch = None
        if not side_closes_position(pre_position, side):
            mismatch = (
                f"--side {side.value} does not close a position of "
                f"{pre_position:g}"
            )
        elif shares > abs(pre_position) + 1e-12:
            mismatch = (
                f"--shares {shares} exceeds the position of {pre_position:g}; "
                "the remainder would open the opposite side"
            )
        if mismatch is not None:
            if not args.allow_position_mismatch:
                raise SystemExit(
                    f"Refusing IBKR manual close: {mismatch}. This tool is "
                    "close-only. Pass --allow-position-mismatch if the broker's "
                    "reported position is the thing you believe is wrong."
                )
            print(f"WARN overriding close-only check: {mismatch}")

        timestamp = datetime.now().astimezone().replace(microsecond=0)
        outcome = adapter.execute(
            build_ibkr_manual_close_plan(
                symbol=symbol,
                shares=shares,
                side=side,
                timestamp=timestamp,
            )
        )
        filled = sum(fill.quantity for fill in getattr(outcome, "fills", ()))
        print(
            f"IBKR manual close: status={outcome.status.value}, "
            f"fills={len(outcome.fills)}, filled_shares={filled:g}, "
            f"message={outcome.message}"
        )

        # Size the expectation from what FILLED, not what was requested, and
        # poll for it. IBKR's portfolio view lags its own fill report, so a
        # single read here returns the PRE-close position and reports a clean
        # close as CRITICAL -- an operator who reruns on that false alarm turns
        # a flat book into a naked position, which is the one outcome this tool
        # exists to prevent.
        filled_shares = int(round(filled))
        expected = pre_position + (
            filled_shares if side == OrderSide.BUY else -filled_shares
        )
        try:
            after_position = fetch_position_with_retry(
                adapter,
                expected="zero" if abs(expected) <= 1e-9 else "nonzero",
            )
            after_open_orders = adapter.fetch_open_orders()
        except Exception as exc:  # noqa: BLE001 - report, never mask
            print(
                f"IBKR manual close after: POSITION UNREADABLE "
                f"({type(exc).__name__}: {exc}) after an order was sent."
            )
            print("CRITICAL manual intervention required")
            print_ibkr_manual_close_hint(symbol, None)
            return 1
        print(
            f"IBKR manual close after: position={after_position:g}, "
            f"open_orders={len(after_open_orders)}"
        )
        if not outcome.filled or after_open_orders:
            print("CRITICAL manual intervention required")
            print_ibkr_manual_close_hint(symbol, after_position)
            return 1
        if abs(after_position - expected) > 1e-6:
            print(
                f"CRITICAL position {after_position:g} does not match "
                f"the expected {expected:g} after a fill of {filled_shares}"
            )
            print_ibkr_manual_close_hint(symbol, after_position)
            return 1
        print(
            f"IBKR manual close complete: position={after_position:g}, "
            f"open_orders={len(after_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


def require_ibkr_manual_close_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    if not str(args.symbol or "").strip():
        raise SystemExit("--symbol is required")
    require_ibkr_symbol(config, args)
    if args.shares is None:
        raise SystemExit("--shares is required for --venue ibkr")
    if int(args.shares) <= 0:
        raise SystemExit("--shares must be positive")
    gates = ibkr_env_gates_open()
    closed = [name for name in ibkr_env_gate_names() if not gates[name]]
    if closed:
        raise SystemExit(
            "IBKR manual close gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def build_ibkr_manual_close_plan(
    *,
    symbol: str,
    shares: int,
    side: OrderSide,
    timestamp: datetime,
) -> PairExecutionPlan:
    # Single-leg by design: the whole point is that the other leg is already
    # where it should be, or gone. `direction` is set to whichever pair
    # direction this side would be closing, so the recorded plan reads
    # coherently rather than carrying a placeholder.
    direction = (
        Direction.SHORT_UMC_LONG_CCF
        if side == OrderSide.BUY
        else Direction.LONG_UMC_SHORT_CCF
    )
    return PairExecutionPlan(
        plan_id=f"IBKR-MANUAL-CLOSE-{timestamp.strftime('%Y%m%d%H%M%S')}",
        plan_type=ExecutionPlanType.EXIT,
        direction=direction,
        timestamp=timestamp,
        row_index=-1,
        legs=(
            ExecutionLeg(
                broker=BrokerName.IBKR_UMC,
                symbol=symbol,
                side=side,
                quantity=float(shares),
                # Unused: the adapter always sends a market order. A real price
                # here would be a fiction the log would later be read as fact.
                price=1.0,
                timestamp=timestamp,
                row_index=-1,
            ),
        ),
        reason="ibkr_manual_close",
        decision_spread_type="manual_close",
    )


def fubon_manual_close(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    require_fubon_manual_close_ready(config, args)

    symbol = str(args.symbol).strip()
    lot = int(args.lot)
    side = OrderSide.BUY if str(args.side).lower() == "buy" else OrderSide.SELL
    adapter = FubonFutureExecutionAdapter(
        symbol,
        config.live.fubon_env_path,
    )
    try:
        pre_position = adapter.fetch_position_quantity()
        pre_open_orders = adapter.fetch_open_orders()
        print_fubon_smoke_position(
            "manual_close_precheck",
            position=pre_position,
            open_orders=len(pre_open_orders),
        )
        print_fubon_smoke_order_records(
            adapter.fetch_order_records(),
            raw_json=bool(args.raw_json),
        )
        if pre_open_orders:
            raise SystemExit(
                "Refusing Fubon manual close with existing open orders: "
                f"{len(pre_open_orders)}"
            )
        # Close-only, on the same terms as the IBKR half. This branch used to
        # print a warning and send anyway, which made the recovery tool
        # reproduce the incident it gets reached for: on 2026-08-19 a `--side
        # buy --lot 1` meant to restore a hedge would have gone out as
        # FutOptOrderType.Close against a position already at zero and been
        # rejected with 8481301, exactly as the automatic emergency close was.
        #
        # Refusing says the true thing -- this tool cannot re-open a hedge --
        # instead of sending an order the exchange is certain to reject.
        mismatch: str | None = None
        if abs(pre_position) <= 1e-12:
            mismatch = (
                "Fubon reports no position in "
                f"{symbol}; there is nothing for --side {args.side} to close"
            )
        elif not side_closes_position(pre_position, side):
            mismatch = (
                f"--side {args.side} does not close a position of "
                f"{pre_position:g}; it would grow or flip it"
            )
        elif lot > abs(pre_position) + 1e-12:
            mismatch = (
                f"--lot {lot} exceeds the position of {abs(pre_position):g}; "
                "the remainder would open the opposite side"
            )
        if mismatch is not None:
            if not bool(getattr(args, "allow_position_mismatch", False)):
                raise SystemExit(
                    f"Refusing Fubon manual close: {mismatch}. This tool is "
                    "close-only. Pass --allow-position-mismatch if the broker's "
                    "reported position is the thing you believe is wrong."
                )
            print(f"WARN overriding close-only check: {mismatch}")

        timestamp = datetime.now().astimezone().replace(microsecond=0)
        close_plan = build_fubon_smoke_plan(
            symbol=symbol,
            lot=lot,
            side=side,
            plan_type=ExecutionPlanType.EXIT,
            timestamp=timestamp,
        )
        outcome = adapter.execute(close_plan)
        print_fubon_smoke_outcome("manual_close", outcome)

        final_open_orders = adapter.fetch_open_orders()
        final_position = fetch_fubon_position_with_retry(
            adapter,
            expected="zero",
        )
        print_fubon_smoke_position(
            "manual_close_after",
            position=final_position,
            open_orders=len(final_open_orders),
        )
        print_fubon_smoke_order_records(
            adapter.fetch_order_records(),
            raw_json=bool(args.raw_json),
        )
        if not outcome.filled or final_open_orders or abs(final_position) > 1e-12:
            print("CRITICAL manual intervention required")
            return 1
        print(
            "Fubon manual close complete: "
            f"position={final_position:g}, open_orders={len(final_open_orders)}"
        )
        return 0
    finally:
        adapter.close()


# ---------------------------------------------------------------------------
# broker-status (read-only: snapshots, funds, order records)
# ---------------------------------------------------------------------------


def command_broker_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    if config.safety.allow_live_order and not (args.funds or args.orders):
        raise SystemExit("allow_live_order must remain false for broker-status")

    if args.orders:
        return fubon_order_records_status(config, args)
    if args.funds:
        return fubon_account_funds_status(config, args)

    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_broker_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    checks = [
        f"store_path={config.store_path}",
        f"reconciliation_enabled={config.broker_reconciliation.enabled}",
        f"fail_on_mismatch={config.broker_reconciliation.fail_on_mismatch}",
        f"umc_units_tolerance={config.broker_reconciliation.umc_units_tolerance}",
        f"ccf_contract_tolerance={config.broker_reconciliation.ccf_contract_tolerance}",
        "private_api=disabled",
    ]
    brokers: tuple[ReadOnlyBroker, ...] = ()
    if helpers.readonly_broker_enabled():
        brokers = helpers.build_real_readonly_brokers(config)
        checks[-1] = "private_api=readonly"
        try:
            for broker in brokers:
                snapshot = broker.fetch_snapshot()
                checks.append(
                    f"{snapshot.broker.value}_snapshot="
                    f"account={snapshot.account_id} "
                    f"positions={len(snapshot.positions)} "
                    f"open_orders={len(snapshot.open_orders)} "
                    f"margins={len(snapshot.margins)}"
                )
        finally:
            helpers.close_brokers(brokers)
    print("Broker status checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def fubon_account_funds_status(config: object, args: argparse.Namespace) -> int:
    if not helpers.readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to query Fubon account funds")

    broker = FubonReadOnlyBroker(config.live.fubon_env_path)
    try:
        snapshot = broker.fetch_snapshot()
    finally:
        broker.close()

    print("Fubon account funds")
    print(f"- account={snapshot.account_id}")
    print(f"- fetched_at={snapshot.fetched_at.isoformat()}")
    print(f"- positions={len(snapshot.positions)}")
    print(f"- open_orders={len(snapshot.open_orders)}")
    if not snapshot.margins:
        print("- margins=0")
        return 0

    print(f"- margins={len(snapshot.margins)}")
    for index, margin in enumerate(snapshot.margins, start=1):
        print(
            f"- margin[{index}] "
            f"currency={margin.currency} "
            f"equity={format_optional_number(margin.equity)} "
            f"available={format_optional_number(margin.available)} "
            f"margin_used={format_optional_number(margin.margin_used)}"
        )
        if args.raw_json:
            print(
                json.dumps(
                    margin.raw or {},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    default=str,
                )
            )
    return 0


def fubon_order_records_status(config: object, args: argparse.Namespace) -> int:
    if not helpers.readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to query Fubon order records")
    symbol = str(args.orders).strip()
    if not symbol:
        raise SystemExit("--orders requires a symbol")

    adapter = FubonFutureExecutionAdapter(
        symbol,
        config.live.fubon_env_path,
    )
    try:
        position = adapter.fetch_position_quantity()
        open_orders = adapter.fetch_open_orders()
        records = adapter.fetch_order_records()
    finally:
        adapter.close()

    print(f"Fubon order records: symbol={symbol}")
    print_fubon_smoke_position(
        "readonly",
        position=position,
        open_orders=len(open_orders),
    )
    print_fubon_smoke_order_records(records, raw_json=bool(args.raw_json))
    return 0


def format_optional_number(value: float | None) -> str:
    if value is None:
        return "NA"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# gate/require helpers
# ---------------------------------------------------------------------------


def require_fubon_exec_smoke_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    if not config.live_execution.enabled:
        raise SystemExit("live_execution.enabled=true is required")
    symbol = str(args.symbol or "").strip()
    if not symbol:
        raise SystemExit("--symbol is required")
    if str(args.confirm_symbol).strip() != symbol:
        raise SystemExit("--confirm-symbol must match --symbol")
    if int(args.lot) <= 0:
        raise SystemExit("--lot must be positive")
    gates = fubon_smoke_env_gates_open()
    closed = [name for name in FUBON_EXECUTION_SMOKE_ENV_GATES if not gates[name]]
    if closed:
        raise SystemExit(
            "Fubon execution smoke gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


def require_fubon_manual_close_ready(
    config: object,
    args: argparse.Namespace,
) -> None:
    if not config.safety.allow_live_order:
        raise SystemExit("safety.allow_live_order=true is required")
    symbol = str(args.symbol or "").strip()
    if not symbol:
        raise SystemExit("--symbol is required")
    if str(args.confirm_symbol).strip() != symbol:
        raise SystemExit("--confirm-symbol must match --symbol")
    if int(args.lot) <= 0:
        raise SystemExit("--lot must be positive")
    gates = fubon_manual_close_env_gates_open()
    closed = [name for name in FUBON_MANUAL_CLOSE_ENV_GATES if not gates[name]]
    if closed:
        raise SystemExit(
            "Fubon manual close gates closed: "
            + ", ".join(f"{name}=1" for name in closed)
        )


# ---------------------------------------------------------------------------
# plan builders / printers / retry
# ---------------------------------------------------------------------------


def fetch_position_with_retry(
    adapter: object,
    *,
    expected: str,
    attempts: int = 5,
    interval_seconds: float = 0.5,
) -> float:
    """Poll until the position reaches the expected shape, or give up.

    Venue-agnostic: both adapters expose fetch_position_quantity(). A broker's
    position view lags its own fill report by a moment, so a single read
    immediately after a fill reports the OLD position and reads as a failure.
    """
    return fetch_fubon_position_with_retry(
        adapter,
        expected=expected,
        attempts=attempts,
        interval_seconds=interval_seconds,
    )


def fetch_fubon_position_with_retry(
    adapter: object,
    *,
    expected: str,
    attempts: int = 5,
    interval_seconds: float = 0.5,
) -> float:
    last = 0.0
    for attempt in range(max(1, attempts)):
        last = float(adapter.fetch_position_quantity())
        if expected == "nonzero" and abs(last) > 1e-12:
            return last
        if expected == "zero" and abs(last) <= 1e-12:
            return last
        if attempt < attempts - 1 and interval_seconds > 0:
            time.sleep(interval_seconds)
    return last


def build_fubon_smoke_plan(
    *,
    symbol: str,
    lot: int,
    side: OrderSide,
    plan_type: ExecutionPlanType,
    timestamp: datetime,
) -> PairExecutionPlan:
    return PairExecutionPlan(
        plan_id=f"FUBON-SMOKE-{timestamp.strftime('%Y%m%d%H%M%S')}-{plan_type.value}",
        plan_type=plan_type,
        direction=Direction.SHORT_UMC_LONG_CCF,
        timestamp=timestamp,
        row_index=-1,
        legs=(
            ExecutionLeg(
                broker=BrokerName.FUBON_CCF,
                symbol=symbol,
                side=side,
                quantity=float(lot),
                price=1.0,
                timestamp=timestamp,
                row_index=-1,
                ccf_symbol=symbol,
            ),
        ),
        reason="fubon_execution_smoke",
        decision_spread_type="manual_smoke",
        ccf_symbol=symbol,
    )


def print_fubon_smoke_outcome(label: str, outcome: object) -> None:
    filled_lot = sum(fill.quantity for fill in getattr(outcome, "fills", ()))
    print(
        "Fubon execution smoke "
        f"{label}: status={outcome.status.value}, "
        f"fills={len(outcome.fills)}, filled_lot={filled_lot:g}, "
        f"message={outcome.message}"
    )


def print_fubon_smoke_position(
    label: str,
    *,
    position: float,
    open_orders: int,
) -> None:
    print(
        "Fubon execution smoke "
        f"{label}: position={position:g}, open_orders={open_orders}"
    )


def print_fubon_smoke_order_records(
    records: tuple[dict[str, object], ...],
    *,
    raw_json: bool,
) -> None:
    print(f"Fubon execution smoke order_records: count={len(records)}")
    for index, record in enumerate(records, start=1):
        print(
            f"- record[{index}] "
            f"order_id={fubon_record_first(record, 'order_no', 'orderNo', 'ord_no', 'seq_no', 'seqNo', 'id') or 'UNKNOWN'} "
            f"symbol={fubon_record_first(record, 'symbol', 'code', 'prod_id', 'stock_no') or 'UNKNOWN'} "
            f"side={fubon_record_first(record, 'buy_sell', 'buySell', 'bs', 'side') or 'UNKNOWN'} "
            f"status={fubon_record_first(record, 'status', 'order_status', 'orderStatus', 'state') or 'UNKNOWN'} "
            f"lot={fubon_record_first(record, 'lot', 'lots', 'quantity', 'qty') or 'NA'} "
            f"filled={fubon_record_first(record, 'filled_lot', 'filledLot', 'match_lot', 'matchLot', 'deal_lot', 'dealLot', 'filled') or 'NA'} "
            f"avg_price={fubon_record_avg_price(record) or 'NA'}"
        )
        if raw_json:
            print(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    default=str,
                )
            )


def fubon_record_first(record: dict[str, object], *names: str) -> object | None:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
        lowered = name.lower()
        for key, value in record.items():
            if str(key).lower() == lowered and value not in (None, ""):
                return value
    return None


def fubon_record_avg_price(record: dict[str, object]) -> object | None:
    direct = fubon_record_first(
        record,
        "average_price",
        "averagePrice",
        "avg_price",
        "avgPrice",
        "match_price",
        "matchPrice",
        "deal_price",
        "dealPrice",
    )
    if direct not in (None, ""):
        return direct
    filled_money = parse_cli_float(
        fubon_record_first(record, "filled_money", "filledMoney")
    )
    filled_lot = parse_cli_float(
        fubon_record_first(record, "filled_lot", "filledLot", "deal_lot", "dealLot")
    )
    if filled_money is None or filled_lot is None or filled_lot == 0:
        return None
    price = filled_money / filled_lot
    if price.is_integer():
        return int(price)
    return price


def parse_cli_float(value: object | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
