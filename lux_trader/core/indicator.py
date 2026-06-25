from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import sqrt
from typing import Any

from .models import IndicatorSnapshot, MarketBar


@dataclass
class IndicatorEngine:
    window: int = 1440
    values: deque[float] = field(default_factory=deque)
    total: float = 0.0
    total_sq: float = 0.0

    def update(self, bar: MarketBar) -> IndicatorSnapshot:
        self.values.append(bar.spread)
        self.total += bar.spread
        self.total_sq += bar.spread * bar.spread
        if len(self.values) > self.window:
            old = self.values.popleft()
            self.total -= old
            self.total_sq -= old * old

        mean: float | None = None
        std: float | None = None
        zscore: float | None = None
        valid = False
        if len(self.values) == self.window:
            mean = self.total / self.window
            variance = max(self.total_sq / self.window - mean * mean, 0.0)
            std = sqrt(variance)
            if std != 0.0:
                zscore = (bar.spread - mean) / std
                valid = True

        return IndicatorSnapshot(
            timestamp=bar.timestamp,
            spread=bar.spread,
            mean=mean,
            std=std,
            zscore=zscore,
            zscore_valid=valid,
            entry_allowed=bar.entry_allowed,
            close_allowed=bar.close_allowed,
            friday_night_close_only=bar.friday_night_close_only,
            weekend_session_close_only=bar.weekend_session_close_only,
            friday_session_end_force_close=bar.friday_session_end_force_close,
        )

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "values": list(self.values),
            "total": self.total,
            "total_sq": self.total_sq,
        }

    @classmethod
    def from_jsonable(cls, payload: dict[str, Any]) -> "IndicatorEngine":
        engine = cls(window=int(payload["window"]))
        engine.values = deque(float(value) for value in payload.get("values", []))
        engine.total = float(payload.get("total", sum(engine.values)))
        engine.total_sq = float(payload.get("total_sq", sum(v * v for v in engine.values)))
        return engine


def validate_expected_zscore(
    bar: MarketBar,
    snapshot: IndicatorSnapshot,
    tolerance: float,
) -> None:
    if bar.expected_zscore_valid != snapshot.zscore_valid:
        raise RuntimeError(
            "Z-score validity mismatch at "
            f"{bar.timestamp}: expected={bar.expected_zscore_valid}, "
            f"actual={snapshot.zscore_valid}"
        )
    if not snapshot.zscore_valid:
        return
    if bar.expected_zscore is None or snapshot.zscore is None:
        raise RuntimeError(f"Missing z-score for valid row at {bar.timestamp}")
    if abs(bar.expected_zscore - snapshot.zscore) > tolerance:
        raise RuntimeError(
            "Z-score mismatch at "
            f"{bar.timestamp}: expected={bar.expected_zscore}, actual={snapshot.zscore}"
        )
