from __future__ import annotations

import sys
from typing import TypeVar

T = TypeVar("T")


def cli_attr(name: str, default: T) -> T:
    module = sys.modules.get("lux_trader.cli")
    if module is None:
        return default
    return getattr(module, name, default)
