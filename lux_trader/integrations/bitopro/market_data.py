from __future__ import annotations

from ..ccxt_market_data import CcxtTickerMarketData


class BitoProMarketData(CcxtTickerMarketData):
    def __init__(self, timeout_ms: int = 30_000) -> None:
        super().__init__("bitopro", timeout_ms=timeout_ms)

