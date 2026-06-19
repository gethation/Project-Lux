from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy
import pandas

from .config import load_config
from .live_market_data import CcxtTickerMarketData, FubonQffMarketData
from .live_runner import (
    LivePaperRunner,
    QffWarmupCheckRunner,
    WarmupRunner,
    resolve_qff_contract,
)
from .models import BrokerName
from .reconciliation import (
    BrokerPositionSnapshot,
    BrokerReconciler,
    FakeReadOnlyBroker,
    ReadOnlyBroker,
    ReconciliationStatus,
)
from .readonly_brokers import BinanceReadOnlyBroker, FubonReadOnlyBroker
from .runner import SystemRunner
from .store import SQLiteStore
from .terminal_ui import LiveTerminalReporter, NullLiveReporter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lux_trader")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="Run CSV replay into SQLite")
    replay.add_argument("--config", type=Path, required=True)
    replay.add_argument("--max-bars", type=int)
    replay.add_argument("--resume", action="store_true")
    replay.add_argument("--reset-store", action="store_true")

    summary = subparsers.add_parser("summary", help="Print SQLite replay summary")
    summary.add_argument("--config", type=Path, required=True)

    doctor = subparsers.add_parser("doctor", help="Check MVP configuration")
    doctor.add_argument("--config", type=Path, required=True)

    broker_doctor = subparsers.add_parser(
        "broker-doctor",
        help="Check read-only broker reconciliation skeleton config",
    )
    broker_doctor.add_argument("--config", type=Path, required=True)

    reconcile_brokers = subparsers.add_parser(
        "reconcile-brokers",
        help="Run read-only broker/store reconciliation",
    )
    reconcile_brokers.add_argument("--config", type=Path, required=True)
    reconcile_brokers.add_argument(
        "--fake",
        action="store_true",
        help="Use deterministic fake read-only brokers",
    )
    reconcile_brokers.add_argument(
        "--readonly",
        action="store_true",
        help="Use real Fubon and Binance read-only brokers",
    )
    reconcile_brokers.add_argument(
        "--fubon-readonly",
        action="store_true",
        help="Use real Fubon read-only broker",
    )
    reconcile_brokers.add_argument(
        "--fake-binance",
        action="store_true",
        help="Use fake Binance broker with real Fubon",
    )
    reconcile_brokers.add_argument(
        "--fake-case",
        choices=("matched", "mismatch", "error"),
        default="matched",
        help="Fake broker scenario for the Phase 3 skeleton",
    )

    live_doctor = subparsers.add_parser("live-doctor", help="Check live-paper config")
    live_doctor.add_argument("--config", type=Path, required=True)

    warmup_live = subparsers.add_parser("warmup-live", help="Seed live-paper warmup bars")
    warmup_live.add_argument("--config", type=Path, required=True)
    warmup_live.add_argument("--reset-store", action="store_true")

    qff_warmup_check = subparsers.add_parser(
        "qff-warmup-check",
        help="Validate Fubon + TAIFEX QFF warmup data",
    )
    qff_warmup_check.add_argument("--config", type=Path, required=True)
    qff_warmup_check.add_argument(
        "--output-csv",
        default=None,
        help="Write comparison CSV to this path; pass an empty string to disable",
    )

    live_paper = subparsers.add_parser("live-paper", help="Run live market data with PaperBroker")
    live_paper.add_argument("--config", type=Path, required=True)
    live_paper.add_argument("--resume", action="store_true")
    live_paper.add_argument("--reset-store", action="store_true")
    live_paper.add_argument("--max-iterations", type=int)
    live_paper.add_argument(
        "--quiet-ui",
        action="store_true",
        help="Disable live terminal UI and print only the final summary",
    )
    live_paper.add_argument(
        "--no-color",
        action="store_true",
        help="Keep live terminal UI but disable ANSI colors",
    )
    live_paper.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Require existing warmup seed bars instead of auto-building them at startup",
    )
    return parser


def command_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("Refusing to run MVP replay with allow_live_order=true")
    result = SystemRunner(config).replay(
        max_bars=args.max_bars,
        resume=args.resume,
        reset_store=args.reset_store,
    )
    print(
        "Replay complete: "
        f"rows_processed={result.rows_processed}, "
        f"start_row={result.start_row}, "
        f"end_row={result.end_row}, "
        f"finalized={result.finalized}"
    )
    return 0


def command_summary(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        print(json.dumps(store.build_summary(config.strategy, config.fees), indent=2))
    finally:
        store.close()
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    checks: list[str] = []
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for this MVP")
    if not config.input_csv.exists():
        raise SystemExit(f"Input CSV does not exist: {config.input_csv}")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    checks.append(f"input_csv={config.input_csv}")
    checks.append(f"store_path={config.store_path}")
    checks.append(f"python={sys.version.split()[0]}")
    checks.append(f"pandas={pandas.__version__}")
    checks.append(f"numpy={numpy.__version__}")
    checks.append(f"live_order={config.safety.allow_live_order}")
    print("Doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def command_broker_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for broker-doctor")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_broker_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    checks = [
        f"store_path={config.store_path}",
        f"reconciliation_enabled={config.broker_reconciliation.enabled}",
        f"fail_on_mismatch={config.broker_reconciliation.fail_on_mismatch}",
        f"tsm_units_tolerance={config.broker_reconciliation.tsm_units_tolerance}",
        f"qff_contract_tolerance={config.broker_reconciliation.qff_contract_tolerance}",
        "private_api=disabled",
    ]
    brokers: tuple[ReadOnlyBroker, ...] = ()
    if readonly_broker_enabled():
        brokers = build_real_readonly_brokers(config)
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
            close_brokers(brokers)
    print("Broker doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def command_reconcile_brokers(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must remain false for reconcile-brokers")

    store = SQLiteStore(config.store_path)
    brokers: tuple[ReadOnlyBroker, ...] = ()
    try:
        store.initialize()
        resume_state = store.load_resume_state()
        strategy_state = resume_state.strategy if resume_state is not None else None
        timestamp = datetime.now().astimezone()
        brokers = build_reconciliation_brokers(
            args,
            config,
            strategy_state,
            timestamp,
        )
        report = BrokerReconciler(
            tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
            qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
        ).reconcile(
            strategy_state=strategy_state,
            brokers=brokers,
            tsm_symbol=config.live.binance_symbol,
            qff_symbol=reconciliation_qff_symbol(config, strategy_state),
            timestamp=timestamp,
        )
        run_id = store.record_reconciliation_report(report)
        store.commit()
    except Exception:
        store.rollback()
        raise
    finally:
        close_brokers(brokers)
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


def build_fake_reconciliation_brokers(
    config: object,
    strategy_state: object,
    *,
    fake_case: str,
    timestamp: datetime,
) -> tuple[FakeReadOnlyBroker, FakeReadOnlyBroker]:
    reconciler = BrokerReconciler(
        tsm_units_tolerance=config.broker_reconciliation.tsm_units_tolerance,
        qff_contract_tolerance=config.broker_reconciliation.qff_contract_tolerance,
    )
    expected = reconciler.expected_from_strategy(
        strategy_state,
        tsm_symbol=config.live.binance_symbol,
        qff_symbol=reconciliation_qff_symbol(config, strategy_state),
        timestamp=timestamp,
    )
    if fake_case == "error":
        return (
            FakeReadOnlyBroker(
                BrokerName.BINANCE_TSM,
                fetch_error=RuntimeError("fake broker fetch failed"),
            ),
            FakeReadOnlyBroker(BrokerName.FUBON_QFF, fetched_at=timestamp),
        )

    tsm_quantity = expected.expected_tsm_units
    qff_quantity = float(expected.expected_qff_contracts)
    if fake_case == "mismatch":
        qff_quantity = qff_quantity + 1.0 if qff_quantity != 0 else 1.0

    tsm_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.BINANCE_TSM,
                symbol=config.live.binance_symbol,
                quantity=tsm_quantity,
            ),
        )
        if tsm_quantity != 0
        else ()
    )
    qff_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.FUBON_QFF,
                symbol=expected.qff_symbol,
                quantity=qff_quantity,
            ),
        )
        if qff_quantity != 0
        else ()
    )
    return (
        FakeReadOnlyBroker(
            BrokerName.BINANCE_TSM,
            account_id="FAKE-BINANCE",
            positions=tsm_positions,
            fetched_at=timestamp,
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON_QFF,
            account_id="FAKE-FUBON",
            positions=qff_positions,
            fetched_at=timestamp,
        ),
    )


def build_reconciliation_brokers(
    args: argparse.Namespace,
    config: object,
    strategy_state: object,
    timestamp: datetime,
) -> tuple[ReadOnlyBroker, ...]:
    if args.fake:
        return build_fake_reconciliation_brokers(
            config,
            strategy_state,
            fake_case=args.fake_case,
            timestamp=timestamp,
        )
    if args.readonly:
        require_readonly_broker_enabled()
        return build_real_readonly_brokers(config)
    if args.fubon_readonly and args.fake_binance:
        require_readonly_broker_enabled()
        fake_binance = build_fake_reconciliation_brokers(
            config,
            strategy_state,
            fake_case=args.fake_case,
            timestamp=timestamp,
        )[0]
        return (
            FubonReadOnlyBroker(config.live.fubon_env_path),
            fake_binance,
        )
    raise SystemExit(
        "Use --fake, --readonly, or --fubon-readonly --fake-binance"
    )


def build_real_readonly_brokers(config: object) -> tuple[ReadOnlyBroker, ReadOnlyBroker]:
    return (
        FubonReadOnlyBroker(config.live.fubon_env_path),
        BinanceReadOnlyBroker(
            config.live.binance_symbol,
            config.live.fubon_env_path,
        ),
    )


def close_brokers(brokers: tuple[ReadOnlyBroker, ...]) -> None:
    for broker in brokers:
        try:
            broker.close()
        except Exception:
            pass


def readonly_broker_enabled() -> bool:
    return os.getenv("LUX_READONLY_BROKER", "").strip() == "1"


def require_readonly_broker_enabled() -> None:
    if not readonly_broker_enabled():
        raise SystemExit("Set LUX_READONLY_BROKER=1 to use real read-only brokers")


def reconciliation_qff_symbol(config: object, strategy_state: object) -> str:
    trading_symbol = getattr(strategy_state, "trading_qff_symbol", None)
    return str(trading_symbol or config.live.qff_symbol)


def command_live_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.safety.allow_live_order:
        raise SystemExit("allow_live_order must be false for live-paper")
    config.store_path.parent.mkdir(parents=True, exist_ok=True)
    probe = config.store_path.parent / ".project_lux_live_write_probe"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    import ccxt

    checks = [
        f"store_path={config.store_path}",
        f"polling_seconds={config.live.polling_seconds}",
        f"warmup_minutes={config.live.warmup_minutes}",
        f"qff_symbol={config.live.qff_symbol}",
        f"binance_symbol={config.live.binance_symbol}",
        f"bitopro_symbol={config.live.bitopro_symbol}",
        f"ccxt={ccxt.__version__}",
        f"live_order={config.safety.allow_live_order}",
    ]

    if os.getenv("LUX_LIVE_MARKETDATA", "").strip() == "1":
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
            session_summaries = getattr(qff, "last_candidate_session_summaries", {})
            if session_summaries:
                checks.append(
                    "qff_candidate_session_summaries="
                    f"{json.dumps(session_summaries, sort_keys=True)}"
                )
            if qff_contract.selection is not None:
                checks.append(
                    "qff_business_days_to_expiry="
                    f"{qff_contract.selection.business_days_to_expiry}"
                )
            qff.ensure_books_subscription(qff_contract.symbol)
            qff_quote = qff.fetch_quote(qff_contract.symbol)
            checks.append(
                "qff_book="
                f"price={qff_quote.price} bid={qff_quote.bid} ask={qff_quote.ask} "
                f"bid_size={qff_quote.bid_size} ask_size={qff_quote.ask_size}"
            )
            binance_quote = CcxtTickerMarketData("binanceusdm").fetch_quote(
                config.live.binance_symbol
            )
            checks.append(
                "binance_book="
                f"price={binance_quote.price} bid={binance_quote.bid} "
                f"ask={binance_quote.ask} bid_size={binance_quote.bid_size} "
                f"ask_size={binance_quote.ask_size}"
            )
            bitopro_quote = CcxtTickerMarketData("bitopro").fetch_quote(
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

    print("Live doctor checks passed")
    for check in checks:
        print(f"- {check}")
    return 0


def command_warmup_live(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = WarmupRunner(config).run(reset_store=args.reset_store)
    print(
        "Warmup complete: "
        f"bars_written={result.bars_written}, "
        f"qff_symbol={result.qff_symbol}, "
        f"start={result.start}, "
        f"end={result.end}"
    )
    return 0


def command_qff_warmup_check(args: argparse.Namespace) -> int:
    if os.getenv("LUX_LIVE_MARKETDATA", "").strip() != "1":
        raise SystemExit("Set LUX_LIVE_MARKETDATA=1 to run qff-warmup-check")
    config = load_config(args.config)
    result = QffWarmupCheckRunner(config).run(output_csv=args.output_csv)
    report = result.report
    print("QFF warmup check passed")
    print(f"- qff_symbol={result.qff_symbol}")
    print(f"- qff_expiry={result.qff_expiry}")
    print(f"- contract_policy_state={result.contract_policy_state}")
    print(f"- start={result.start}")
    print(f"- end={result.end}")
    print(f"- qff_fetch_start={result.qff_fetch_start}")
    print(f"- rows={len(report.frame)}")
    print(f"- source_rows={json.dumps(report.source_rows, sort_keys=True)}")
    print(
        f"- source_used_counts={json.dumps(report.source_used_counts, sort_keys=True)}"
    )
    print(f"- qff_close_filled_nulls={report.null_count}")
    print(f"- overlap_rows={report.overlap_rows}")
    print(f"- overlap_mismatch_count={report.mismatch_count}")
    print(f"- overlap_mismatch_max_abs_diff={report.max_abs_diff}")
    if result.output_csv is not None:
        print(f"- output_csv={result.output_csv}")
    return 0


def command_live_paper(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    reporter = (
        NullLiveReporter()
        if args.quiet_ui
        else LiveTerminalReporter(color=False if args.no_color else None)
    )
    try:
        result = LivePaperRunner(config, reporter=reporter).run(
            resume=args.resume,
            reset_store=args.reset_store,
            max_iterations=args.max_iterations,
            skip_warmup=args.skip_warmup,
        )
    except Exception as exc:
        reporter.error(datetime.now().astimezone(), f"{type(exc).__name__}: {exc}")
        raise
    reporter.finish()
    print(
        "Live-paper stopped: "
        f"iterations={result.iterations}, "
        f"bars_processed={result.bars_processed}, "
        f"skipped_minutes={result.skipped_minutes}, "
        f"qff_symbol={result.qff_symbol}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "replay":
        return command_replay(args)
    if args.command == "summary":
        return command_summary(args)
    if args.command == "doctor":
        return command_doctor(args)
    if args.command == "broker-doctor":
        return command_broker_doctor(args)
    if args.command == "reconcile-brokers":
        return command_reconcile_brokers(args)
    if args.command == "live-doctor":
        return command_live_doctor(args)
    if args.command == "warmup-live":
        return command_warmup_live(args)
    if args.command == "qff-warmup-check":
        return command_qff_warmup_check(args)
    if args.command == "live-paper":
        return command_live_paper(args)
    parser.error(f"Unknown command: {args.command}")
    return 2
