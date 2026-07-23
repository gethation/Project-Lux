"""Shared test fakes.

``build_fake_reconciliation_brokers`` was a CLI helper in the legacy project
(behind ``--fake`` flags); the rebuilt CLI only exposes real read-only brokers,
so the fake pair-broker builder lives here and is injected into commands by
monkeypatching ``lux_trader.cli.commands_live.build_reconciliation_brokers``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from lux_trader.core.models import BrokerName, Direction, OrderRequest, OrderSide
from lux_trader.execution.intent import (
    ExecutionPlanType,
    pair_execution_plan_from_order_requests,
)
from lux_trader.reconciliation import (
    BrokerPositionSnapshot,
    BrokerReconciler,
    FakeReadOnlyBroker,
)


def reconciliation_tw_leg_symbol(config: object, strategy_state: object) -> str:
    trading_symbol = getattr(strategy_state, "trading_tw_leg_symbol", None)
    return str(trading_symbol or config.live.tw_leg_symbol)


def build_fake_reconciliation_brokers(
    config: object,
    strategy_state: object,
    *,
    fake_case: str,
    timestamp: datetime,
) -> tuple[FakeReadOnlyBroker, FakeReadOnlyBroker]:
    reconciler = BrokerReconciler(
        us_leg_units_tolerance=config.broker_reconciliation.us_leg_units_tolerance,
        tw_leg_contract_tolerance=config.broker_reconciliation.tw_leg_contract_tolerance,
    )
    expected = reconciler.expected_from_strategy(
        strategy_state,
        us_leg_symbol=config.live.binance_symbol,
        tw_leg_symbol=reconciliation_tw_leg_symbol(config, strategy_state),
        timestamp=timestamp,
    )
    if fake_case == "error":
        return (
            FakeReadOnlyBroker(
                BrokerName.BINANCE,
                fetch_error=RuntimeError("fake broker fetch failed"),
            ),
            FakeReadOnlyBroker(BrokerName.FUBON, fetched_at=timestamp),
        )

    us_leg_quantity = expected.expected_us_leg_units
    tw_leg_quantity = float(expected.expected_tw_leg_contracts)
    if fake_case == "mismatch":
        tw_leg_quantity = tw_leg_quantity + 1.0 if tw_leg_quantity != 0 else 1.0

    us_leg_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.BINANCE,
                symbol=config.live.binance_symbol,
                quantity=us_leg_quantity,
            ),
        )
        if us_leg_quantity != 0
        else ()
    )
    tw_leg_positions = (
        (
            BrokerPositionSnapshot(
                broker=BrokerName.FUBON,
                symbol=expected.tw_leg_symbol,
                quantity=tw_leg_quantity,
            ),
        )
        if tw_leg_quantity != 0
        else ()
    )
    return (
        FakeReadOnlyBroker(
            BrokerName.BINANCE,
            account_id="FAKE-BINANCE",
            positions=us_leg_positions,
            fetched_at=timestamp,
        ),
        FakeReadOnlyBroker(
            BrokerName.FUBON,
            account_id="FAKE-FUBON",
            positions=tw_leg_positions,
            fetched_at=timestamp,
        ),
    )


def build_fake_execution_plan(
    config: object,
    *,
    fake_case: str,
    timestamp: datetime,
    row_index: int,
):
    tw_leg_symbol = str(config.live.tw_leg_symbol)
    if tw_leg_symbol.lower() == "auto":
        tw_leg_symbol = "QFFG6"
    binance_side = OrderSide.SELL
    if fake_case == "rejected":
        binance_side = OrderSide.BUY
    requests = (
        OrderRequest(
            broker=BrokerName.BINANCE,
            symbol=config.live.binance_symbol,
            side=binance_side,
            quantity=125.5,
            price=720.0,
            timestamp=timestamp,
            row_index=row_index,
            fee_twd=12.3,
            tw_leg_symbol=tw_leg_symbol,
            tw_leg_expiry="2026-02-18",
            contract_policy_state="fake",
        ),
        OrderRequest(
            broker=BrokerName.FUBON,
            symbol=tw_leg_symbol,
            side=OrderSide.BUY,
            quantity=3,
            price=1180.0,
            timestamp=timestamp,
            row_index=row_index,
            fee_twd=45.6,
            tw_leg_symbol=tw_leg_symbol,
            tw_leg_expiry="2026-02-18",
            contract_policy_state="fake",
        ),
    )
    return pair_execution_plan_from_order_requests(
        plan_type=ExecutionPlanType.ENTRY,
        direction=Direction.SHORT_US_LONG_TW,
        requests=requests,
        reason=f"fake_{fake_case}",
        decision_zscore=2.14,
        decision_spread_type="shortSpread",
    )


def make_fake_broker_builder(fake_case: str):
    """Return a drop-in replacement for commands_live.build_reconciliation_brokers."""

    def builder(config, strategy_state, *, readonly):  # noqa: ARG001 - CLI seam
        return build_fake_reconciliation_brokers(
            config,
            strategy_state,
            fake_case=fake_case,
            timestamp=datetime.now().astimezone(),
        )

    return builder


def write_test_config(
    tmp_path: Path,
    *,
    allow_live_order: bool | None = None,
    tw_leg_lots: int | None = None,
    tw_leg_symbol: str = "QFFG6",
    include_broker_reconciliation: bool = False,
    margin_enabled: bool | None = None,
    margin_leg_notional_twd: float | None = None,
) -> Path:
    """Write the shared minimal config used by CLI/integration tests."""

    config_path = tmp_path / "config.test.toml"
    store_path = (tmp_path / "project_lux.sqlite3").as_posix()
    cache_dir = (tmp_path / "taifex_cache").as_posix()
    lines = [
        "[paths]",
        f"store_path = '{store_path}'",
        "",
        "[[pairs]]",
        "id = 'qff_tsm'",
        "label = 'QFF/TSM'",
        "",
        "[pairs.data]",
        "input_csv = ''",
        "",
        "[pairs.tw_leg]",
        "display = 'QFF'",
        "venue = 'fubon'",
        "product = 'QFF'",
        f"symbol = '{tw_leg_symbol}'",
        "contract_multiplier = 100.0",
        "",
        "[pairs.us_leg]",
        "display = 'TSM'",
        "venue = 'binance'",
        "symbol = 'TSM/USDT:USDT'",
        "adr_share_ratio = 5.0",
        "",
        "[pairs.fx]",
        "venue = 'bitopro'",
        "symbol = 'USDT/TWD'",
        "",
        "[pairs.sizing]",
        f"mode = '{'fixed_lots' if tw_leg_lots is not None else 'notional'}'",
    ]
    if tw_leg_lots is None:
        lines.append("leg_notional_twd = 1000000.0")
    else:
        lines.append(f"lots = {tw_leg_lots}")
    lines.extend(["", "[pairs.strategy]", "", "[pairs.fees]"])
    if allow_live_order is not None:
        lines.extend(
            [
                "",
                "[safety]",
                f"allow_live_order = {str(allow_live_order).lower()}",
            ]
        )
    lines.extend(
        [
            "",
            "[live_market_data]",
            f"taifex_cache_dir = '{cache_dir}'",
        ]
    )
    if include_broker_reconciliation:
        lines.extend(
            [
                "",
                "[broker_reconciliation]",
                "enabled = false",
                "fail_on_mismatch = false",
                "us_leg_units_tolerance = 0.000001",
                "tw_leg_contract_tolerance = 0",
            ]
        )
    if margin_enabled is not None:
        lines.extend(
            [
                "",
                "[margin_management]",
                f"enabled = {str(margin_enabled).lower()}",
                "check_time = '10:00'",
                "red_line_interval_minutes = 15",
            ]
        )
        if margin_leg_notional_twd is not None:
            lines.append(f"leg_notional_twd = {margin_leg_notional_twd}")
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def write_execution_test_config(
    tmp_path: Path,
    *,
    config_name: str = "config.test.toml",
    store_name: str = "store.sqlite3",
    cache_name: str = "taifex",
    allow_live_order: bool = True,
    live_execution_enabled: bool = True,
    include_broker_reconciliation: bool = False,
    fubon_env_path: str | None = ".env",
) -> Path:
    """Write the common live-execution/admin CLI test configuration."""

    config_path = tmp_path / config_name
    lines = [
        "[paths]",
        f"store_path = '{(tmp_path / store_name).as_posix()}'",
        "",
        "[[pairs]]",
        "id = 'qff_tsm'",
        "label = 'QFF/TSM'",
        "",
        "[pairs.data]",
        "input_csv = ''",
        "",
        "[pairs.tw_leg]",
        "display = 'QFF'",
        "venue = 'fubon'",
        "product = 'QFF'",
        "symbol = 'QFFG6'",
        "contract_multiplier = 100.0",
        "",
        "[pairs.us_leg]",
        "display = 'TSM'",
        "venue = 'binance'",
        "symbol = 'TSM/USDT:USDT'",
        "adr_share_ratio = 5.0",
        "",
        "[pairs.fx]",
        "venue = 'bitopro'",
        "symbol = 'USDT/TWD'",
        "",
        "[pairs.sizing]",
        "mode = 'notional'",
        "leg_notional_twd = 1000000.0",
        "",
        "[pairs.strategy]",
        "",
        "[pairs.fees]",
        "",
        "[safety]",
        f"allow_live_order = {str(allow_live_order).lower()}",
        "",
        "[live_market_data]",
    ]
    if fubon_env_path is not None:
        lines.append(f"fubon_env_path = '{fubon_env_path}'")
    lines.append(f"taifex_cache_dir = '{(tmp_path / cache_name).as_posix()}'")
    if include_broker_reconciliation:
        lines.extend(
            [
                "",
                "[broker_reconciliation]",
                "enabled = true",
                "fail_on_mismatch = true",
                "us_leg_units_tolerance = 0.000001",
                "tw_leg_contract_tolerance = 0",
            ]
        )
    lines.extend(
        [
            "",
            "[live_execution]",
            f"enabled = {str(live_execution_enabled).lower()}",
            "require_readonly_reconciliation = true",
            "max_plan_age_seconds = 120",
            "tw_leg_first = true",
        ]
    )
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path

