"""Shared CLI helpers for broker construction and env gates.

The rebuilt CLI only exposes real read-only brokers (`--readonly`); fake
brokers live in test fixtures and are injected by monkeypatching
``build_reconciliation_brokers`` in the command modules.
"""

from __future__ import annotations

import os

from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker
from lux_trader.integrations.venues import open_umc_readonly_broker
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


def reconciliation_ccf_symbol(config: object, strategy_state: object) -> str:
    trading_symbol = getattr(strategy_state, "trading_ccf_symbol", None)
    return str(trading_symbol or config.live.ccf_symbol)


def build_real_readonly_brokers(
    config: object,
    *,
    ccf_symbol: str | None = None,
) -> tuple[ReadOnlyBroker, ReadOnlyBroker]:
    fubon_symbol = None
    if ccf_symbol and str(ccf_symbol).strip().lower() != "auto":
        fubon_symbol = str(ccf_symbol).strip()
    return (
        FubonReadOnlyBroker(config.live.fubon_env_path, symbol=fubon_symbol),
        open_umc_readonly_broker(
            config.live.umc_symbol,
            config.live.fubon_env_path,
            config,
        ),
    )


def build_umc_readonly_broker(config: object, *, readonly: bool):
    """The IBKR read-only broker alone, for commands that need no Fubon view."""
    if not readonly:
        raise SystemExit("Pass --readonly to use the real IBKR read-only broker")
    require_readonly_broker_enabled()
    return open_umc_readonly_broker(
        config.live.umc_symbol,
        config.live.fubon_env_path,
        config,
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
        ccf_symbol=reconciliation_ccf_symbol(config, strategy_state),
    )


def close_brokers(brokers: tuple[ReadOnlyBroker, ...]) -> None:
    for broker in brokers:
        try:
            broker.close()
        except Exception:
            pass
