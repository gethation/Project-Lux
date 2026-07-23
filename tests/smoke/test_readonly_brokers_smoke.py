from __future__ import annotations

import os
from pathlib import Path

import pytest

from lux_trader.config import load_config
from lux_trader.integrations.binance.readonly import BinanceReadOnlyBroker
from lux_trader.integrations.fubon.readonly import FubonReadOnlyBroker


pytestmark = pytest.mark.readonly_broker


def smoke_config():
    if os.getenv("LUX_READONLY_BROKER", "").strip() != "1":
        pytest.skip("Set LUX_READONLY_BROKER=1 to run read-only broker smoke tests")
    config_path = Path("configs/live.example.toml")
    if not config_path.exists():
        pytest.skip(f"Config does not exist: {config_path}")
    config = load_config(config_path)
    if config.safety.allow_live_order:
        pytest.fail("Read-only smoke requires allow_live_order=false")
    return config


def test_fubon_readonly_smoke() -> None:
    config = smoke_config()
    broker = FubonReadOnlyBroker(config.live.fubon_env_path)
    try:
        snapshot = broker.fetch_snapshot()
    finally:
        broker.close()

    assert snapshot.broker.value == "FUBON"
    assert snapshot.account_id
    assert not snapshot.account_id.isdigit()
    assert len(snapshot.margins) >= 0
    assert len(snapshot.positions) >= 0
    assert len(snapshot.open_orders) >= 0


def test_binance_readonly_smoke() -> None:
    config = smoke_config()
    broker = BinanceReadOnlyBroker(
        config.live.binance_symbol,
        config.live.fubon_env_path,
    )
    try:
        snapshot = broker.fetch_snapshot()
    finally:
        broker.close()

    assert snapshot.broker.value == "BINANCE"
    assert snapshot.account_id == "BINANCE_USDM"
    assert len(snapshot.margins) >= 0
    assert len(snapshot.positions) >= 0
    assert len(snapshot.open_orders) >= 0
