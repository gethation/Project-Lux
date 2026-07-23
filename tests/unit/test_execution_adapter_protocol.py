from __future__ import annotations

from lux_trader.execution import (
    ExecutionAdapter,
    ExecutionPreflight,
    OrderRecordsProvider,
    SessionHealthProvider,
)
from lux_trader.integrations.binance.execution import (
    BinanceExecutionPreflight,
    BinanceUsLegExecutionAdapter,
)
from lux_trader.integrations.fubon.execution import (
    FubonExecutionPreflight,
    FubonFutureExecutionAdapter,
)
from lux_trader.integrations.fubon.execution_process import (
    FubonFutureExecutionProcess,
)


def test_execution_adapters_share_the_formal_protocol() -> None:
    adapters = (
        BinanceUsLegExecutionAdapter("TSM/USDT:USDT"),
        FubonFutureExecutionAdapter("TMFG6"),
        FubonFutureExecutionProcess("TMFG6"),
    )
    try:
        assert all(isinstance(adapter, ExecutionAdapter) for adapter in adapters)
    finally:
        for adapter in adapters:
            adapter.close()


def test_execution_preflight_type_is_shared_without_losing_fields() -> None:
    assert BinanceExecutionPreflight is ExecutionPreflight
    assert FubonExecutionPreflight is ExecutionPreflight
    assert ExecutionPreflight(open_orders=(), position_quantity=0.0) == (
        BinanceExecutionPreflight(open_orders=(), position_quantity=0.0)
    )


def test_fubon_only_capabilities_stay_on_narrow_protocols() -> None:
    adapter = FubonFutureExecutionAdapter("TMFG6")
    try:
        assert isinstance(adapter, OrderRecordsProvider)
        assert isinstance(adapter, SessionHealthProvider)
        assert not isinstance(
            BinanceUsLegExecutionAdapter("TSM/USDT:USDT"), OrderRecordsProvider
        )
    finally:
        adapter.close()
