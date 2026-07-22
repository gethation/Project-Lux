"""Live-mode CLI commands: dry-run, status, recovery, reconciliation, warmup.

Rebuilt thin shell around the frozen live runtime. Compared to legacy:
- no live-paper mode, no ``--fake`` flags (fakes live in tests),
- one reporter factory serving ``--ui compact|dashboard`` plus ``--quiet-ui``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from lux_trader.cli import helpers
from lux_trader.config import load_config
from lux_trader.core.calendar import live_session_status
from lux_trader.core.models import StrategyState
from lux_trader.core.time import ensure_taipei
from lux_trader.dashboard_ui import DashboardReporter
from lux_trader.ntfy import NtfyLiveReporter
from lux_trader.reconciliation import (
    BrokerReconciler,
    ReadOnlyBroker,
    ReconciliationStatus,
)
from lux_trader.reconciliation.post_trade import PostTradeReconciler
from lux_trader.runtime.live import LiveDryRunRunner, WarmupRunner, resolve_qff_contract
from lux_trader.runtime.live.lease import assert_live_lease_available
from lux_trader.store import SQLiteStore
from lux_trader.terminal_ui import LiveTerminalReporter, NullLiveReporter


def with_ntfy(reporter: object, config: object, *, mode: str):
    if config.ntfy.enabled:
        return NtfyLiveReporter(
            reporter,
            config.ntfy,
            mode=mode,
            store_path=config.store_path,
        )
    return reporter


def build_live_reporter(args: argparse.Namespace, config: object, *, mode: str):
    """Reporter factory shared by live modes: compact (default), dashboard, quiet."""
    if getattr(args, "quiet_ui", False):
        return with_ntfy(NullLiveReporter(), config, mode=mode)
    color = False if getattr(args, "no_color", False) else None
    ui = getattr(args, "ui", "compact")
    if ui == "dashboard" and not sys.stdout.isatty():
        print(
            "note: stdout is not a terminal; falling back to --ui compact",
            file=sys.stderr,
        )
        ui = "compact"
    if ui == "compact":
        return with_ntfy(LiveTerminalReporter(color=color), config, mode=mode)
    gate_text = (
        "allow_live_order=false · simulated adapter (DRYRUN-*)"
        if mode == "live-dry-run"
        else None
    )
    return with_ntfy(
        DashboardReporter(
            mode=mode,
            qff_symbol=config.live.qff_symbol,
            binance_symbol=config.live.binance_symbol,
            bitopro_symbol=config.live.bitopro_symbol,
            gate_text=gate_text,
            color=color,
        ),
        config,
        mode=mode,
    )


def command_live_dry_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for live-dry-run")
    reporter = build_live_reporter(args, config, mode="live-dry-run")
    try:
        result = LiveDryRunRunner(config, reporter=reporter).run(
            resume=args.resume,
            reset_store=args.reset_store,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except Exception as exc:
        reporter.error(
            datetime.now().astimezone(), f"{type(exc).__name__}: {exc}"
        )
        raise
    finally:
        reporter.finish()
    print(
        "Live dry-run stopped: "
        f"iterations={result.iterations}, "
        f"bars_processed={result.bars_processed}, "
        f"skipped_minutes={result.skipped_minutes}, "
        f"plans_recorded={result.plans_recorded}, "
        f"qff_symbol={result.qff_symbol}"
    )
    return 0


def command_live_status(args: argparse.Namespace) -> int:
    """Read-only operator snapshot: persisted strategy state, position, and
    latest reconciliation. Sends no orders and touches no external API."""
    config = load_config(args.config)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        resume_state = store.load_resume_state()
        report = store.load_latest_reconciliation_report()
        fubon_health = store.load_latest_fubon_session_health()
    finally:
        store.close()

    print(f"Live status: store={config.store_path}")
    if resume_state is None:
        print("- strategy_state: none (no persisted strategy state yet)")
    else:
        state = resume_state.strategy
        direction = (
            state.position_direction.value if state.position_direction else "none"
        )
        print(f"- strategy_state: {state.state.value}")
        print(f"- row_index: {resume_state.row_index}")
        print(
            "- position: "
            f"direction={direction}, "
            f"tsm_units={state.tsm_units}, "
            f"qff_contracts={state.qff_contracts}, "
            f"qff_symbol={state.trading_qff_symbol or '-'}"
        )
        print(f"- realized_pnl_twd: {state.realized_pnl}")
        if state.pnl_status != "complete":
            print(
                "- realized_pnl_status: pending "
                "(excludes externally manual-closed trade)"
            )
        if state.state == StrategyState.PAUSED:
            print(
                "- ACTION: strategy is PAUSED; inspect, manual-close any stray leg, "
                "then run clear-pause once reconciliation matches"
            )
    if report is None:
        print("- reconciliation: none recorded")
    else:
        print(
            "- reconciliation: "
            f"status={report.status.value}, issues={len(report.issues)}"
        )
        for issue in report.issues:
            print(
                f"  - {issue.status.value} {issue.issue_type} "
                f"{issue.broker.value} {issue.symbol or '-'} {issue.message}"
            )
    if fubon_health is None:
        print("- fubon_session: none recorded")
    else:
        print(
            "- fubon_session: "
            f"status={fubon_health['status']}, "
            f"generation={fubon_health['generation']}, "
            f"worker_pid={fubon_health['worker_pid'] or '-'}, "
            f"last_login={fubon_health['last_login_at'] or '-'}, "
            f"last_success={fubon_health['last_success_at'] or '-'}, "
            f"relogin_count={fubon_health['relogin_count']}, "
            f"invalid_reason={fubon_health['invalid_reason'] or '-'}"
        )
    return 0




def command_reconcile_brokers(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        run_id, report = reconcile_brokers_to_store(
            config,
            store,
            readonly=bool(args.readonly),
        )
    finally:
        store.close()

    print(
        "Broker reconciliation complete: "
        f"run_id={run_id}, status={report.status.value}, issues={len(report.issues)}"
    )
    for issue in report.issues:
        print(
            f"- {issue.status.value} {issue.issue_type} "
            f"{issue.broker.value} {issue.symbol or '-'} {issue.message}"
        )
    return 1 if report.status == ReconciliationStatus.ERROR else 0


def reconcile_brokers_to_store(
    config: object,
    store: SQLiteStore,
    *,
    readonly: bool,
    timestamp: datetime | None = None,
    brokers: tuple[ReadOnlyBroker, ...] | None = None,
):
    """Fetch read-only broker state, reconcile it, and persist one report.

    The caller owns ``store``. Real broker construction remains guarded by both
    the explicit ``readonly`` argument and ``LUX_READONLY_BROKER=1``.
    """
    active_brokers: tuple[ReadOnlyBroker, ...] = brokers or ()
    owns_brokers = brokers is None
    try:
        resume_state = store.load_resume_state()
        strategy_state = resume_state.strategy if resume_state is not None else None
        observed_at = timestamp or datetime.now().astimezone()
        if not active_brokers:
            active_brokers = build_reconciliation_brokers(
                config,
                strategy_state,
                readonly=readonly,
            )
        report = BrokerReconciler(
            tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
            qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
        ).reconcile(
            strategy_state=strategy_state,
            brokers=active_brokers,
            tsm_symbol=config.live.binance_symbol,
            qff_symbol=helpers.reconciliation_qff_symbol(config, strategy_state),
            timestamp=observed_at,
        )
        run_id = store.record_reconciliation_report(report)
        store.commit()
        return run_id, report
    except Exception:
        store.rollback()
        raise
    finally:
        if owns_brokers:
            helpers.close_brokers(active_brokers)


def command_clear_pause(args: argparse.Namespace) -> int:
    """Guarded recovery: re-run read-only reconciliation and only clear a PAUSED
    strategy back to OPEN/FLAT when broker and store agree. Sends no orders."""
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    store = SQLiteStore(config.store_path)
    brokers: tuple[ReadOnlyBroker, ...] = ()
    target: StrategyState | None = None
    try:
        store.initialize()
        resume_state = store.load_resume_state()
        if resume_state is None:
            raise SystemExit("No persisted strategy state to clear")
        state = resume_state.strategy
        if state.state != StrategyState.PAUSED:
            print(
                f"Strategy state is {state.state.value}, not paused; nothing to clear"
            )
            return 0

        timestamp = datetime.now().astimezone()
        brokers = build_reconciliation_brokers(
            config,
            state,
            readonly=bool(args.readonly),
        )
        pending_manual_close = store.load_pending_manual_close()
        if pending_manual_close is not None:
            report = PostTradeReconciler(
                tsm_units_tolerance=(
                    config.broker_reconciliation.tsm_units_tolerance
                ),
                qff_contract_tolerance=(
                    config.broker_reconciliation.qff_contract_tolerance
                ),
            ).reconcile(
                store=store,
                strategy_state=state,
                brokers=brokers,
                tsm_symbol=config.live.binance_symbol,
                qff_symbol=helpers.reconciliation_qff_symbol(config, state),
                timestamp=timestamp,
            )
        else:
            report = BrokerReconciler(
                tsm_units_tolerance=(
                    config.broker_reconciliation.tsm_units_tolerance
                ),
                qff_contract_tolerance=(
                    config.broker_reconciliation.qff_contract_tolerance
                ),
            ).reconcile(
                strategy_state=state,
                brokers=brokers,
                tsm_symbol=config.live.binance_symbol,
                qff_symbol=helpers.reconciliation_qff_symbol(config, state),
                timestamp=timestamp,
            )
        store.record_reconciliation_report(report)
        if report.status != ReconciliationStatus.MATCHED:
            store.commit()
            print(
                "Refusing clear-pause: reconciliation status="
                f"{report.status.value}, issues={len(report.issues)}"
            )
            for issue in report.issues:
                print(
                    f"- {issue.status.value} {issue.issue_type} "
                    f"{issue.broker.value} {issue.symbol or '-'} {issue.message}"
                )
            return 1

        has_position = (
            state.position_direction is not None
            or abs(float(state.tsm_units or 0.0)) > 1e-12
            or int(state.qff_contracts or 0) != 0
        )
        target = StrategyState.OPEN if has_position else StrategyState.FLAT
        state.state = target
        store.save_state(
            resume_state.row_index,
            timestamp,
            state,
            resume_state.indicator,
        )
        store.record_event(
            resume_state.row_index,
            timestamp,
            "clear_pause",
            f"manual clear-pause -> {target.value}",
            {
                "reconciliation_status": report.status.value,
                "target_state": target.value,
            },
        )
        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        helpers.close_brokers(brokers)
        store.close()

    print(f"Cleared PAUSED -> {target.value} after matched reconciliation")
    return 0


def command_margin_check(args: argparse.Namespace) -> int:
    """One-shot dual-account margin check with transfer guidance (read-only).

    Same policy as the in-loop daily 10:00 check; suitable for running from a
    Windows scheduled task when no live session is up.
    """
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    if not config.margin_management.enabled:
        raise SystemExit("Set [margin_management] enabled=true to run margin-check")
    helpers.require_readonly_broker_enabled()

    from lux_trader.core.models import StrategyState as _StrategyState
    from lux_trader.margin.monitor import POSITION_OPEN_STATES
    from lux_trader.margin.service import (
        MarginCheckService,
        resolve_margin_leg_notional_twd,
    )

    store = SQLiteStore(config.store_path)
    brokers: tuple[ReadOnlyBroker, ...] = ()
    try:
        store.initialize()
        resume_state = store.load_resume_state()
        position_open = (
            resume_state is not None
            and resume_state.strategy.state in POSITION_OPEN_STATES
        )
        brokers = build_margin_brokers(config)
        decision = MarginCheckService(
            config,
            brokers=brokers,
            usdttwd_rate=lambda: fetch_usdttwd_rate(config),
        ).run_check(
            check_type="daily",
            position_open=position_open,
            leg_notional_twd=resolve_margin_leg_notional_twd(config, store),
        )
        check_id = store.record_margin_check(decision)
        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        helpers.close_brokers(brokers)
        store.close()

    print(f"Margin check complete: check_id={check_id}, level={decision.level}")
    for assessment in (decision.binance, decision.fubon):
        equity = (
            f"{assessment.equity_twd:,.0f}"
            if assessment.equity_twd is not None
            else "NA"
        )
        maint = (
            f"{assessment.maint_margin_twd:,.0f}"
            if assessment.maint_margin_twd is not None
            else "NA"
        )
        ratio = f"{assessment.ratio:.1%}" if assessment.ratio is not None else "NA"
        print(
            f"- {assessment.venue}: equity_twd={equity}, "
            f"maint_margin_twd={maint}, ratio={ratio}, level={assessment.level}"
        )
    if decision.usdttwd_rate is not None:
        print(f"- usdttwd_rate={decision.usdttwd_rate}")
    print(f"- guidance: {decision.guidance}")
    return 1 if decision.level == "red_line" else 0


def fetch_usdttwd_rate(config: object) -> float | None:
    from lux_trader.integrations.bitopro.market_data import BitoProMarketData

    try:
        quote = BitoProMarketData().fetch_quote(config.live.bitopro_symbol)
        return getattr(quote, "price", None)
    except Exception as exc:
        print(f"WARN usdttwd rate unavailable: {type(exc).__name__}: {exc}")
        return None


def command_warmup_live(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    result = WarmupRunner(config).run(reset_store=args.reset_store)
    print(
        "Warmup complete: "
        f"bars_written={result.bars_written}, "
        f"qff_symbol={result.qff_symbol}, "
        f"start={result.start}, "
        f"end={result.end}"
    )
    return 0


def live_session_label(session_status: object) -> str:
    is_trading = bool(getattr(session_status, "is_trading"))
    is_close_only = bool(getattr(session_status, "is_close_only"))
    if not is_trading:
        return "closed"
    if is_close_only:
        return "close_only"
    return "open"


def qff_book_diagnostic_lines(
    qff_quote: object,
    observed_at: datetime,
    stale_seconds: float,
) -> list[str]:
    quote_timestamp = ensure_taipei(getattr(qff_quote, "timestamp"))
    age_sec = max((ensure_taipei(observed_at) - quote_timestamp).total_seconds(), 0.0)
    stale = age_sec > stale_seconds
    lines = [
        f"qff_book_timestamp={quote_timestamp.isoformat()}",
        f"qff_book_age_sec={age_sec:.3f}",
        f"qff_book_stale={str(stale).lower()}",
    ]
    if stale:
        lines.append(
            f"WARN stale_qff_book age_sec={age_sec:.3f} threshold={stale_seconds}"
        )
    return lines


def run_live_doctor_checks(config: object) -> list[str]:
    """Live-mode doctor: config/session checks; touches real market data only
    when LUX_LIVE_MARKETDATA=1 is explicitly set (deterministic by default)."""
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for doctor --mode live")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_live_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    import ccxt

    observed_at = ensure_taipei(datetime.now().astimezone())
    session_status = live_session_status(
        observed_at,
        config.trading_calendar.closed_dates,
    )

    checks = [
        f"store_path={config.store_path}",
        f"polling_seconds={config.live.polling_seconds}",
        f"warmup_minutes={config.live.warmup_minutes}",
        f"qff_symbol={config.live.qff_symbol}",
        f"binance_symbol={config.live.binance_symbol}",
        f"bitopro_symbol={config.live.bitopro_symbol}",
        f"live_session={live_session_label(session_status)}",
        f"next_trading_start={session_status.next_open_at.isoformat()}",
        f"ccxt={ccxt.__version__}",
        f"live_order={config.safety.allow_live_order}",
    ]

    if helpers.live_marketdata_enabled():
        from lux_trader.integrations.binance.market_data import BinanceMarketData
        from lux_trader.integrations.bitopro.market_data import BitoProMarketData
        from lux_trader.integrations.fubon.market_data import FubonQffMarketData

        qff = FubonQffMarketData(config.live.fubon_env_path)
        try:
            qff_contract = resolve_qff_contract(config, qff)
            checks.append(f"qff_active_symbol={qff_contract.symbol}")
            checks.append(f"qff_active_expiry={qff_contract.expiry}")
            checks.append(f"qff_contract_policy={qff_contract.policy_state}")
            session_counts = getattr(qff, "last_candidate_session_counts", {})
            if session_counts:
                checks.append(
                    "qff_candidate_session_counts="
                    f"{json.dumps(session_counts, sort_keys=True)}"
                )
            if qff_contract.selection is not None:
                checks.append(
                    "qff_business_days_to_expiry="
                    f"{qff_contract.selection.business_days_to_expiry}"
                )
            try:
                qff.ensure_books_subscription(qff_contract.symbol)
                qff_quote = qff.fetch_quote(qff_contract.symbol)
                checks.append(
                    "qff_book="
                    f"price={qff_quote.price} bid={qff_quote.bid} ask={qff_quote.ask} "
                    f"bid_size={qff_quote.bid_size} ask_size={qff_quote.ask_size}"
                )
                checks.extend(
                    qff_book_diagnostic_lines(
                        qff_quote,
                        observed_at,
                        config.live.qff_book_stale_seconds,
                    )
                )
            except Exception as exc:
                checks.append(
                    "WARN qff_book_unavailable "
                    f"{type(exc).__name__}: {exc}"
                )
            binance_quote = BinanceMarketData().fetch_quote(
                config.live.binance_symbol
            )
            checks.append(
                "binance_book="
                f"price={binance_quote.price} bid={binance_quote.bid} "
                f"ask={binance_quote.ask} bid_size={binance_quote.bid_size} "
                f"ask_size={binance_quote.ask_size}"
            )
            bitopro_quote = BitoProMarketData().fetch_quote(
                config.live.bitopro_symbol
            )
            checks.append(
                "bitopro_book="
                f"price={bitopro_quote.price} bid={bitopro_quote.bid} "
                f"ask={bitopro_quote.ask} bid_size={bitopro_quote.bid_size} "
                f"ask_size={bitopro_quote.ask_size}"
            )
        finally:
            qff.close()
    else:
        checks.append(
            f"live_marketdata=disabled (set {helpers.LIVE_MARKETDATA_ENV}=1 "
            "for real provider checks)"
        )
    return checks


# Test seams: fakes are injected by monkeypatching these names.
build_reconciliation_brokers = helpers.build_reconciliation_brokers


def build_margin_brokers(config: object):
    from lux_trader.margin.monitor import build_default_margin_brokers

    return build_default_margin_brokers(config)
