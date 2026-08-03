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
from lux_trader.runtime.live import LiveDryRunRunner, WarmupRunner, resolve_ccf_contract
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
            ccf_symbol=config.live.ccf_symbol,
            umc_symbol=config.live.umc_symbol,
            fx_symbol=config.live.fx_symbol,
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
        f"ccf_symbol={result.ccf_symbol}"
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
            f"umc_units={state.umc_units}, "
            f"ccf_contracts={state.ccf_contracts}, "
            f"ccf_symbol={state.trading_ccf_symbol or '-'}"
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
            umc_units_tolerance=config.broker_reconciliation.umc_units_tolerance,
            ccf_contract_tolerance=config.broker_reconciliation.ccf_contract_tolerance,
        ).reconcile(
            strategy_state=strategy_state,
            brokers=active_brokers,
            umc_symbol=config.live.umc_symbol,
            ccf_symbol=helpers.reconciliation_ccf_symbol(config, strategy_state),
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
                umc_units_tolerance=(
                    config.broker_reconciliation.umc_units_tolerance
                ),
                ccf_contract_tolerance=(
                    config.broker_reconciliation.ccf_contract_tolerance
                ),
            ).reconcile(
                store=store,
                strategy_state=state,
                brokers=brokers,
                umc_symbol=config.live.umc_symbol,
                ccf_symbol=helpers.reconciliation_ccf_symbol(config, state),
                timestamp=timestamp,
            )
        else:
            report = BrokerReconciler(
                umc_units_tolerance=(
                    config.broker_reconciliation.umc_units_tolerance
                ),
                ccf_contract_tolerance=(
                    config.broker_reconciliation.ccf_contract_tolerance
                ),
            ).reconcile(
                strategy_state=state,
                brokers=brokers,
                umc_symbol=config.live.umc_symbol,
                ccf_symbol=helpers.reconciliation_ccf_symbol(config, state),
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
            or abs(float(state.umc_units or 0.0)) > 1e-12
            or int(state.ccf_contracts or 0) != 0
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
            usd_twd_rate=lambda: fetch_usd_twd_rate(config),
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
    for assessment in (decision.umc, decision.fubon):
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
    if decision.usd_twd_rate is not None:
        print(f"- usd_twd_rate={decision.usd_twd_rate}")
    print(f"- guidance: {decision.guidance}")
    return 1 if decision.level == "red_line" else 0


def fetch_usd_twd_rate(config: object) -> float | None:
    from lux_trader.integrations.venues import open_fx_quote_provider

    try:
        quote = open_fx_quote_provider(config).fetch_quote(config.live.fx_symbol)
        return getattr(quote, "price", None)
    except Exception as exc:
        print(f"WARN usd_twd rate unavailable: {type(exc).__name__}: {exc}")
        return None


def command_warmup_live(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    assert_live_lease_available(config.store_path)
    result = WarmupRunner(config).run(reset_store=args.reset_store)
    print(
        "Warmup complete: "
        f"bars_written={result.bars_written}, "
        f"ccf_symbol={result.ccf_symbol}, "
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


def ccf_book_diagnostic_lines(
    ccf_quote: object,
    observed_at: datetime,
    stale_seconds: float,
) -> list[str]:
    quote_timestamp = ensure_taipei(getattr(ccf_quote, "timestamp"))
    age_sec = max((ensure_taipei(observed_at) - quote_timestamp).total_seconds(), 0.0)
    stale = age_sec > stale_seconds
    lines = [
        f"ccf_book_timestamp={quote_timestamp.isoformat()}",
        f"ccf_book_age_sec={age_sec:.3f}",
        f"ccf_book_stale={str(stale).lower()}",
    ]
    if stale:
        lines.append(
            f"WARN stale_ccf_book age_sec={age_sec:.3f} threshold={stale_seconds}"
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

    observed_at = ensure_taipei(datetime.now().astimezone())
    session_status = live_session_status(
        observed_at,
        config.trading_calendar.closed_dates,
    )

    checks = [
        f"store_path={config.store_path}",
        f"polling_seconds={config.live.polling_seconds}",
        f"warmup_minutes={config.live.warmup_minutes}",
        f"ccf_symbol={config.live.ccf_symbol}",
        f"umc_symbol={config.live.umc_symbol}",
        f"fx_symbol={config.live.fx_symbol}",
        f"live_session={live_session_label(session_status)}",
        f"next_trading_start={session_status.next_open_at.isoformat()}",
        f"live_order={config.safety.allow_live_order}",
    ]

    if helpers.live_marketdata_enabled():
        from lux_trader.integrations.fubon.market_data import FubonCcfMarketData
        from lux_trader.integrations.venues import (
            open_fx_quote_provider,
            open_umc_quote_provider,
        )

        ccf = FubonCcfMarketData(config.live.fubon_env_path)
        try:
            ccf_contract = resolve_ccf_contract(config, ccf)
            checks.append(f"ccf_active_symbol={ccf_contract.symbol}")
            checks.append(f"ccf_active_expiry={ccf_contract.expiry}")
            checks.append(f"ccf_contract_policy={ccf_contract.policy_state}")
            session_counts = getattr(ccf, "last_candidate_session_counts", {})
            if session_counts:
                checks.append(
                    "ccf_candidate_session_counts="
                    f"{json.dumps(session_counts, sort_keys=True)}"
                )
            if ccf_contract.selection is not None:
                checks.append(
                    "ccf_business_days_to_expiry="
                    f"{ccf_contract.selection.business_days_to_expiry}"
                )
            try:
                ccf.ensure_books_subscription(ccf_contract.symbol)
                ccf_quote = ccf.fetch_quote(ccf_contract.symbol)
                checks.append(
                    "ccf_book="
                    f"price={ccf_quote.price} bid={ccf_quote.bid} ask={ccf_quote.ask} "
                    f"bid_size={ccf_quote.bid_size} ask_size={ccf_quote.ask_size}"
                )
                checks.extend(
                    ccf_book_diagnostic_lines(
                        ccf_quote,
                        observed_at,
                        config.live.ccf_book_stale_seconds,
                    )
                )
            except Exception as exc:
                checks.append(
                    "WARN ccf_book_unavailable "
                    f"{type(exc).__name__}: {exc}"
                )
            # The UMC and FX venues are unwired until Phase B. Report that as a
            # check line rather than aborting: doctor's job is to say what is
            # and is not working, and the CCF half above is real information.
            try:
                umc_quote = open_umc_quote_provider(config).fetch_quote(
                    config.live.umc_symbol
                )
                checks.append(
                    "umc_book="
                    f"price={umc_quote.price} bid={umc_quote.bid} "
                    f"ask={umc_quote.ask} bid_size={umc_quote.bid_size} "
                    f"ask_size={umc_quote.ask_size}"
                )
            except Exception as exc:
                checks.append(f"WARN umc_book_unavailable {type(exc).__name__}: {exc}")
            try:
                fx_quote = open_fx_quote_provider(config).fetch_quote(
                    config.live.fx_symbol
                )
                checks.append(
                    "fx_book="
                    f"price={fx_quote.price} bid={fx_quote.bid} "
                    f"ask={fx_quote.ask} bid_size={fx_quote.bid_size} "
                    f"ask_size={fx_quote.ask_size}"
                )
            except Exception as exc:
                checks.append(f"WARN fx_book_unavailable {type(exc).__name__}: {exc}")
        finally:
            ccf.close()
    else:
        checks.append(
            f"live_marketdata=disabled (set {helpers.LIVE_MARKETDATA_ENV}=1 "
            "for real provider checks)"
        )
    return checks


def run_ibkr_doctor_checks(config: object) -> tuple[bool, list[str]]:
    """Answer one question: can the UMC leg actually produce a signal today?

    Returns (passed, lines). Passing needs BOTH a live tier and a real book --
    a connected session serving `last` with no bid/ask is the failure mode this
    command exists to catch, because the system starts, runs all session, logs
    `missing_book` on every bar, and trades nothing. That looks like a quiet
    market, not a broken entitlement.

    Unlike `--mode live`, this has nothing to do without a socket, so an unset
    LUX_LIVE_MARKETDATA is refused rather than silently reported as a no-op.
    """
    from lux_trader.integrations.ibkr.diagnostic import (
        IbkrDiagnosticConfig,
        run_connectivity_diagnostic,
    )

    if not helpers.live_marketdata_enabled():
        raise SystemExit(
            f"status doctor --mode ibkr needs {helpers.LIVE_MARKETDATA_ENV}=1: "
            "it is a real read-only connection to IB Gateway and does nothing "
            "without one"
        )

    result = run_connectivity_diagnostic(
        IbkrDiagnosticConfig(
            host=config.live.ibkr_host,
            port=config.live.ibkr_port,
            # Deliberately NOT config.live.ibkr_client_id: that id belongs to the
            # live quote worker, and a doctor run must never contend with a
            # running session for it.
            symbol=config.live.umc_symbol,
            market_data_type=config.live.ibkr_market_data_type,
        )
    )

    lines = [
        f"gateway={result.host}:{result.port} client_id={result.client_id}",
        f"server_version={result.server_version} accounts={list(result.accounts)}",
        f"contract={result.symbol} con_id={result.con_id} "
        f"{result.exchange}/{result.primary_exchange} {result.currency}",
        f"market_data_requested={result.market_data_type_requested} "
        f"granted={result.market_data_tier} ({result.market_data_tier_label})",
        f"book=bid {result.quote_bid} / ask {result.quote_ask} "
        f"size {result.quote_bid_size} / {result.quote_ask_size}",
        f"last={result.quote_last} close={result.quote_close}",
        f"shortable_shares={result.shortable_shares} "
        f"rank={result.shortable_rank} borrowable={result.shortable}",
        f"historical_1m_bars={result.historical_bar_count}",
    ]

    passed = result.book_available and result.live_tier_granted
    if not result.live_tier_granted:
        lines.append(
            f"FAIL market data tier is {result.market_data_tier_label}, not live"
        )
    if not result.book_available:
        lines.append(
            "FAIL no bid/ask: the directional z-score cannot be computed, so "
            "the pair would take zero entries all session"
        )
    if result.entitlement_error_code is not None:
        lines.append(
            f"FAIL entitlement error {result.entitlement_error_code}: "
            f"{result.entitlement_error_message}"
        )
    elif not result.book_available:
        # No entitlement error and no book points somewhere else entirely.
        lines.append(
            "NOTE no entitlement error was reported, so a closed market or a "
            "dead feed is more likely than a missing subscription"
        )
    if result.shortable is False:
        lines.append(
            f"WARN {result.symbol} is not borrowable right now (rank "
            f"{result.shortable_rank}); about half of this strategy's trades "
            "sell it first"
        )
    if result.historical_error_code is not None:
        lines.append(
            f"WARN historical data error {result.historical_error_code}: "
            f"{result.historical_error_message}"
        )
    return passed, lines


# Test seams: fakes are injected by monkeypatching these names.
build_reconciliation_brokers = helpers.build_reconciliation_brokers


def build_margin_brokers(config: object):
    from lux_trader.margin.monitor import build_default_margin_brokers

    return build_default_margin_brokers(config)
