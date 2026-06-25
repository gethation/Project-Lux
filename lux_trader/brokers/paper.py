from __future__ import annotations

from itertools import count

from lux_trader.core.broker import Broker
from lux_trader.core.models import Fill, OrderRequest, OrderResult, OrderStatus


class PaperBroker(Broker):
    def __init__(self) -> None:
        self._order_counter = count(1)
        self._fill_counter = count(1)
        self._open_orders: dict[str, OrderResult] = {}

    def get_position(self) -> object | None:
        return None

    def place_order(self, request: OrderRequest) -> tuple[OrderResult, Fill]:
        order_id = f"PAPER-{request.row_index:08d}-{next(self._order_counter):04d}"
        fill_id = f"FILL-{request.row_index:08d}-{next(self._fill_counter):04d}"
        result = OrderResult(order_id=order_id, request=request, status=OrderStatus.FILLED)
        fill = Fill(
            fill_id=fill_id,
            order_id=order_id,
            broker=request.broker,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            fee_twd=request.fee_twd,
            timestamp=request.timestamp,
            row_index=request.row_index,
            qff_symbol=request.qff_symbol,
            qff_expiry=request.qff_expiry,
            contract_policy_state=request.contract_policy_state,
        )
        return result, fill

    def get_open_orders(self) -> list[OrderResult]:
        return list(self._open_orders.values())

    def cancel_order(self, order_id: str) -> OrderResult | None:
        return self._open_orders.pop(order_id, None)
