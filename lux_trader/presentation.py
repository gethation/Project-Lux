from __future__ import annotations

import re
from typing import Any


_TW_LEG_TERM = re.compile(r"\btw[_ -]?leg\b", re.IGNORECASE)
_US_LEG_TERM = re.compile(r"\bus[_ -]?leg\b", re.IGNORECASE)


def instrument_text(
    value: Any,
    *,
    tw_leg_display: str,
    us_leg_display: str,
) -> str:
    """Translate internal neutral leg terminology at the presentation boundary."""

    text = str(value)
    text = _TW_LEG_TERM.sub(str(tw_leg_display), text)
    return _US_LEG_TERM.sub(str(us_leg_display), text)


def direction_text(
    value: Any,
    *,
    tw_leg_display: str,
    us_leg_display: str,
) -> str:
    raw = str(getattr(value, "value", value) or "").lower()
    if raw == "short_us_long_tw":
        return f"Short {us_leg_display} / Long {tw_leg_display}"
    if raw == "long_us_short_tw":
        return f"Long {us_leg_display} / Short {tw_leg_display}"
    return instrument_text(
        getattr(value, "value", value),
        tw_leg_display=tw_leg_display,
        us_leg_display=us_leg_display,
    )


def metric_label(display: str, suffix: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", str(display)).strip("_").lower()
    return f"{normalized or 'instrument'}_{suffix}"
