from __future__ import annotations

from ..ccxt_market_data import CcxtTickerMarketData


class BinanceMarketData(CcxtTickerMarketData):
    def __init__(self, timeout_ms: int = 30_000) -> None:
        super().__init__("binanceusdm", timeout_ms=timeout_ms)

