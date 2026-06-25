from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pandas as pd


def third_wednesday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_wednesday_offset = (2 - first.weekday()) % 7
    return first + timedelta(days=first_wednesday_offset + 14)


def row_get(row: Any, *names: str) -> Any:
    if isinstance(row, dict):
        for name in names:
            if name in row:
                return row[name]
            lowered = name.lower()
            for key, value in row.items():
                if str(key).lower() == lowered:
                    return value
        return None
    for name in names:
        if hasattr(row, name):
            return getattr(row, name)
    return None


def row_to_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "__dict__"):
        return dict(row.__dict__)
    return {"value": str(row)}


def parse_contract_expiry(raw: dict[str, Any], product: str) -> date | None:
    for key in (
        "expiry",
        "expirationDate",
        "endDate",
        "settlementDate",
        "lastTradeDate",
        "deliveryDate",
        "maturityDate",
    ):
        value = row_get(raw, key)
        if value:
            try:
                return pd.Timestamp(str(value)).date()
            except Exception:
                pass

    for key in ("contract_month", "contractMonth", "deliveryMonth", "maturityMonth"):
        value = row_get(raw, key)
        if value:
            match = re.search(r"(20\d{2})(0[1-9]|1[0-2])", str(value))
            if match:
                return third_wednesday(int(match.group(1)), int(match.group(2)))

    symbol = str(row_get(raw, "symbol", "code", "id", "ticker") or "")
    match = re.search(rf"{re.escape(product)}.*?(20\d{{2}})(0[1-9]|1[0-2])", symbol)
    if match:
        return third_wednesday(int(match.group(1)), int(match.group(2)))
    return None

