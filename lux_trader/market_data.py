from __future__ import annotations

from pathlib import Path

import pandas as pd

from .calendar import TradingCalendar
from .models import MarketBar

TAIPEI_TZ = "Asia/Taipei"


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def optional_float(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return float(parsed)


class CsvReplayMarketData:
    def __init__(self, csv_path: Path, calendar: TradingCalendar | None = None) -> None:
        self.csv_path = csv_path
        self.calendar = calendar or TradingCalendar()

    def load(self) -> list[MarketBar]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Input CSV does not exist: {self.csv_path}")

        frame = pd.read_csv(self.csv_path)
        required = {
            "timestamp",
            "qff_close",
            "qff_close_filled",
            "tsm_twd_fair",
            "spread",
            "spread_zscore",
            "zscore_valid",
        }
        missing = required.difference(frame.columns)
        if missing:
            raise RuntimeError(
                f"{self.csv_path} is missing required columns: {sorted(missing)}"
            )
        if frame.empty:
            raise RuntimeError(f"Input CSV has no rows: {self.csv_path}")

        timestamps = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
            TAIPEI_TZ
        )
        expected = pd.date_range(timestamps.iloc[0], timestamps.iloc[-1], freq="min")
        index = pd.DatetimeIndex(timestamps)
        if not index.is_unique or not index.is_monotonic_increasing:
            raise RuntimeError("Input timestamps must be unique and sorted")
        if len(index) != len(expected) or not index.equals(expected):
            raise RuntimeError("Input CSV must be a continuous 1m series")

        bars: list[MarketBar] = []
        for row_index, row in frame.iterrows():
            qff_close = optional_float(row["qff_close"])
            qff_close_filled = optional_float(row["qff_close_filled"])
            tsm_twd_fair = optional_float(row["tsm_twd_fair"])
            spread = optional_float(row["spread"])
            if qff_close_filled is None or tsm_twd_fair is None or spread is None:
                raise RuntimeError(f"Invalid market data at row {row_index}")
            bars.append(
                MarketBar(
                    row_index=int(row_index),
                    timestamp=timestamps.iloc[row_index].to_pydatetime(),
                    qff_close=qff_close,
                    qff_close_filled=qff_close_filled,
                    tsm_twd_fair=tsm_twd_fair,
                    spread=spread,
                    expected_zscore=optional_float(row["spread_zscore"]),
                    expected_zscore_valid=parse_bool(row["zscore_valid"]),
                )
            )
        return self.calendar.annotate(bars)
