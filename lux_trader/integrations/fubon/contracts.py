from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ...core.contracts import row_to_dict
from ...core.models import OrderSide
from .parsing import fubon_first_float, fubon_first_text, fubon_raw_row


MONTH_CODES = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "I": 9,
    "J": 10,
    "K": 11,
    "L": 12,
}
ORDER_MONTH_CODES = {month: code for code, month in MONTH_CODES.items()}
FUBON_MONTH_CODES = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}
FUBON_PRODUCT_ALIASES = {
    "TMF": {"TMF", "FITM"},
    "QFF": {"QFF", "FIQFF"},
}
FUBON_BROKER_PRODUCTS = {
    "FITM": "TMF",
    "FIQFF": "QFF",
}


@dataclass(frozen=True)
class FubonContractIdentity:
    requested_symbol: str
    product: str
    contract_month: str | None
    broker_symbols: frozenset[str]

    @classmethod
    def from_symbol(
        cls,
        symbol: str,
        *,
        reference_date: date | None = None,
    ) -> FubonContractIdentity:
        requested = str(symbol or "").strip().upper()
        product = product_prefix_from_symbol(requested) or requested
        aliases = set(FUBON_PRODUCT_ALIASES.get(product, {product}))
        aliases.add(requested)
        return cls(
            requested_symbol=requested,
            product=product,
            contract_month=contract_month_from_symbol(
                requested,
                reference_date=reference_date,
            ),
            broker_symbols=frozenset(
                alias.upper()
                for alias in aliases
                if alias
            ),
        )

    def matches(
        self,
        row: Any,
        *,
        side: OrderSide | str | None = None,
        lot: float | int | None = None,
    ) -> bool:
        raw = fubon_raw_row(row)
        actual_symbol = (fubon_symbol(raw) or "").strip().upper()
        if not actual_symbol:
            return False

        symbol_matches = actual_symbol == self.requested_symbol
        if not symbol_matches and actual_symbol in self.broker_symbols:
            row_month = fubon_contract_month(raw)
            symbol_matches = (
                self.contract_month is None
                or row_month == self.contract_month
            )
        if not symbol_matches:
            return False
        if side is not None and not fubon_side_matches(raw, side):
            return False
        if lot is not None and not fubon_lot_matches(raw, lot):
            return False
        return True


def fubon_symbol(row: Any) -> str | None:
    raw = row_to_dict(row)
    return fubon_first_text(
        raw,
        "symbol",
        "code",
        "id",
        "ticker",
        "stock_no",
        "prod_id",
    )


def fubon_symbol_matches(row: Any, requested_symbol: str) -> bool:
    return FubonContractIdentity.from_symbol(requested_symbol).matches(row)


def normalize_fubon_order_symbol(
    symbol: str,
    *,
    product: str | None = None,
    expiry: date | str | None = None,
    reference_date: date | None = None,
) -> str:
    """Return the FutOptOrder symbol format accepted by Fubon's trading API."""
    requested = str(symbol or "").strip().upper()
    if not requested:
        return requested
    order_product = (
        str(product or "").strip().upper()
        or product_prefix_from_symbol(requested)
        or product_from_numeric_symbol(requested)
    )
    contract_month = (
        contract_month_from_expiry(expiry)
        or contract_month_from_symbol(requested, reference_date=reference_date)
        or contract_month_from_numeric_symbol(requested)
    )
    if not order_product or contract_month is None:
        return requested
    month = int(contract_month[4:6])
    month_code = ORDER_MONTH_CODES.get(month)
    if month_code is None:
        return requested
    year_digit = int(contract_month[:4]) % 10
    return f"{order_product}{month_code}{year_digit}"


def product_prefix_from_symbol(symbol: str) -> str | None:
    normalized = symbol.strip().upper()
    for broker_prefix, product in FUBON_BROKER_PRODUCTS.items():
        if normalized.startswith(broker_prefix):
            return product
    match = re.fullmatch(r"([A-Z]+)([A-L])(\d)", normalized)
    return match.group(1) if match is not None else None


def product_from_numeric_symbol(symbol: str) -> str | None:
    match = re.fullmatch(r"([A-Z]+)20\d{2}(0[1-9]|1[0-2])", symbol.strip().upper())
    return match.group(1) if match is not None else None


def contract_month_from_expiry(value: date | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return f"{value.year}{value.month:02d}"
    digits = re.sub(r"\D", "", str(value))
    return digits[:6] if len(digits) >= 6 else None


def contract_month_from_numeric_symbol(symbol: str) -> str | None:
    match = re.search(r"(20\d{2})(0[1-9]|1[0-2])", symbol.strip().upper())
    return f"{match.group(1)}{match.group(2)}" if match is not None else None


def contract_month_from_symbol(
    symbol: str,
    *,
    reference_date: date | None = None,
) -> str | None:
    normalized = symbol.strip().upper()
    reference = reference_date or datetime.now().astimezone().date()
    fubon_match = re.fullmatch(r"(FI[A-Z]+)([FGHJKMNQUVXZ])(\d{2})", normalized)
    if fubon_match is not None:
        code_month = FUBON_MONTH_CODES.get(fubon_match.group(2))
        numeric_tail = int(fubon_match.group(3))
        month = (
            numeric_tail
            if 1 <= numeric_tail <= 12 and numeric_tail == code_month
            else code_month
        )
        if month is None:
            return None
        year = reference.year
        if month < reference.month:
            year += 1
        return f"{year}{month:02d}"

    match = re.fullmatch(r"([A-Z]+)([A-L])(\d)", normalized)
    if match is None:
        return None
    month = MONTH_CODES.get(match.group(2))
    if month is None:
        return None
    year_digit = int(match.group(3))
    decade = reference.year - reference.year % 10
    year = decade + year_digit
    while year < reference.year - 1:
        year += 10
    return f"{year}{month:02d}"


def fubon_contract_month(row: dict[str, Any]) -> str | None:
    value = fubon_first_text(
        row,
        "expiry_date",
        "expiryDate",
        "contract_month",
        "contractMonth",
        "settlement_month",
        "settlementMonth",
    )
    if value is None:
        return None
    digits = re.sub(r"\D", "", value)
    return digits[:6] if len(digits) >= 6 else None


def fubon_side_matches(row: Any, side: OrderSide | str) -> bool:
    expected = normalize_side_text(side)
    actual = normalize_side_text(
        fubon_first_text(
            row_to_dict(row),
            "buy_sell",
            "buySell",
            "bs",
            "side",
        )
    )
    return expected is not None and actual == expected


def normalize_side_text(side: OrderSide | str | None) -> str | None:
    text = str(getattr(side, "value", side) or "").strip().lower()
    if text in {"buy", "b", "1", "long"} or "buy" in text:
        return "buy"
    if text in {"sell", "s", "2", "short"} or "sell" in text:
        return "sell"
    return None


def fubon_lot_matches(row: Any, lot: float | int) -> bool:
    actual = fubon_first_float(
        row_to_dict(row),
        "lot",
        "lots",
        "quantity",
        "qty",
    )
    return actual is not None and abs(actual - float(lot)) <= 1e-12
