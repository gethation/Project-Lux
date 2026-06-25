from __future__ import annotations

import json
from typing import Any

from ...core.contracts import row_get, row_to_dict
from ...market_data.parsing import parse_optional_float
from ..serialization import safe_jsonable


def fubon_raw_row(row: Any) -> dict[str, Any]:
    raw = safe_jsonable(row_to_dict(row))
    if isinstance(raw, dict):
        return unwrap_fubon_value(raw)
    return {"value": raw}


def unwrap_fubon_value(raw: dict[str, Any]) -> dict[str, Any]:
    value = raw.get("value")
    if len(raw) == 1 and isinstance(value, str):
        parsed = parse_json_object(value) or parse_fubon_repr_object(value)
        if parsed is not None:
            return parsed
    if isinstance(value, str):
        parsed = parse_json_object(value) or parse_fubon_repr_object(value)
        if parsed is not None:
            merged = dict(raw)
            merged.pop("value", None)
            merged.update(parsed)
            return merged
    return raw


def parse_json_object(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return safe_jsonable(parsed)
    return None


def parse_fubon_repr_object(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if "{" not in text or "}" not in text:
        return None
    body = text[text.find("{") + 1 : text.rfind("}")]
    parsed: dict[str, Any] = {}
    for line in body.splitlines():
        item = line.strip().rstrip(",")
        if not item or ":" not in item:
            continue
        key, raw_value = item.split(":", 1)
        parsed[key.strip()] = parse_fubon_repr_value(raw_value.strip())
    return parsed or None


def parse_fubon_repr_value(value: str) -> Any:
    if value == "None":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    numeric = parse_optional_float(value)
    if numeric is not None:
        return int(numeric) if numeric.is_integer() else numeric
    return value


def fubon_first_text(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = row_get(row, name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def fubon_first_float(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = parse_optional_float(row_get(row, name))
        if value is not None:
            return value
    return None


def apply_side_sign(quantity: float, side: str | None) -> float:
    text = str(side or "").strip().lower()
    if "sell" in text or "short" in text or text in {"s", "2"}:
        return -abs(quantity)
    if "buy" in text or "long" in text or text in {"b", "1"}:
        return abs(quantity)
    return quantity

