from __future__ import annotations

from abc import ABC, abstractmethod

from lux_trader.models import Fill, OrderRequest, OrderResult


class Broker(ABC):
    @abstractmethod
    def get_position(self) -> object | None:
        raise NotImplementedError

    @abstractmethod
    def place_order(self, request: OrderRequest) -> tuple[OrderResult, Fill]:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(self) -> list[OrderResult]:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult | None:
        raise NotImplementedError
