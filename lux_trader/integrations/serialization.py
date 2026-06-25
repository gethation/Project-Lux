from __future__ import annotations

from datetime import datetime
from typing import Any


def safe_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): safe_jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [safe_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return safe_jsonable(getattr(value, "value"))
    return repr(value)

