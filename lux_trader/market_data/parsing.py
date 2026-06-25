from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..core.contracts import row_get
from ..core.time import TAIPEI_TZ, ensure_taipei


def parse_timestamp(value: Any) -> datetime:
    if value is None:
        return datetime.now(TAIPEI_TZ)
    if isinstance(value, datetime):
        return ensure_taipei(value)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000_000_000:
            return datetime.fromtimestamp(raw / 1_000_000_000, tz=TAIPEI_TZ)
        if raw > 10_000_000_000_000:
            return datetime.fromtimestamp(raw / 1_000_000, tz=TAIPEI_TZ)
        if raw > 10_000_000_000:
            return datetime.fromtimestamp(raw / 1000, tz=TAIPEI_TZ)
        return datetime.fromtimestamp(raw, tz=TAIPEI_TZ)
    text = str(value).strip()
    if not text:
        return datetime.now(TAIPEI_TZ)
    return ensure_taipei(pd.Timestamp(text).to_pydatetime())


def parse_optional_float(value: Any) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return float(parsed)


def first_book_level(levels: Any) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    level = levels if isinstance(levels, dict) else levels[0]
    if isinstance(level, dict):
        return (
            parse_optional_float(row_get(level, "price", "px")),
            parse_optional_float(
                row_get(level, "size", "amount", "qty", "quantity")
            ),
        )
    if isinstance(level, (list, tuple)):
        price = parse_optional_float(level[0]) if len(level) >= 1 else None
        size = parse_optional_float(level[1]) if len(level) >= 2 else None
        return price, size
    return None, None


def midpoint_or_single_side(
    bid: float | None,
    ask: float | None,
) -> float | None:
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


def first_float(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        parsed = parse_optional_float(row_get(row, name))
        if parsed is not None:
            return parsed
    return None

