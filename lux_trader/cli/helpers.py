"""Shared CLI helpers for broker construction and env gates.

The rebuilt CLI only exposes real read-only brokers (`--readonly`); fake
brokers live in test fixtures and are injected by monkeypatching
``build_reconciliation_brokers`` in the command modules.
"""

from __future__ import annotations

import os

from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.reconciliation import ReadOnlyBroker


LIVE_MARKETDATA_ENV = "LUX_LIVE_MARKETDATA"
READONLY_BROKER_ENV = "LUX_READONLY_BROKER"


def live_marketdata_enabled() -> bool:
    return os.getenv(LIVE_MARKETDATA_ENV, "").strip() == "1"


def readonly_broker_enabled() -> bool:
    return os.getenv(READONLY_BROKER_ENV, "").strip() == "1"


def require_readonly_broker_enabled() -> None:
    if not readonly_broker_enabled():
        raise SystemExit(
            f"Set {READONLY_BROKER_ENV}=1 to use real read-only brokers"
        )


def reconciliation_tw_leg_symbol(config: object, strategy_state: object) -> str:
    trading_symbol = getattr(strategy_state, "trading_tw_leg_symbol", None)
    return str(trading_symbol or config.live.tw_leg_symbol)


def build_real_readonly_brokers(
    config: object,
    *,
    tw_leg_symbol: str | None = None,
) -> tuple[ReadOnlyBroker, ReadOnlyBroker]:
    fubon_symbol = None
    if tw_leg_symbol and str(tw_leg_symbol).strip().lower() != "auto":
        fubon_symbol = str(tw_leg_symbol).strip()
    return (
        FubonReadOnlyBroker(config.live.fubon_env_path, symbol=fubon_symbol),
        BinanceReadOnlyBroker(
            config.live.binance_symbol,
            config.live.fubon_env_path,
        ),
    )


def build_reconciliation_brokers(
    config: object,
    strategy_state: object,
    *,
    readonly: bool,
) -> tuple[ReadOnlyBroker, ...]:
    if not readonly:
        raise SystemExit("Pass --readonly to use real read-only brokers")
    require_readonly_broker_enabled()
    return build_real_readonly_brokers(
        config,
        tw_leg_symbol=reconciliation_tw_leg_symbol(config, strategy_state),
    )


def close_brokers(brokers: tuple[ReadOnlyBroker, ...]) -> None:
    for broker in brokers:
        try:
            broker.close()
        except Exception:
            pass
