from __future__ import annotations

from pathlib import Path

import pandas as pd

from ..core.calendar import TradingCalendar
from ..core.models import MarketBar

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
    def __init__(
        self,
        csv_path: Path,
        calendar: TradingCalendar | None = None,
        *,
        qff_ohlcv_path: Path | None = None,
        tsm_ohlcv_path: Path | None = None,
        usdttwd_ohlcv_path: Path | None = None,
    ) -> None:
        self.csv_path = csv_path
        self.calendar = calendar or TradingCalendar()
        self.qff_ohlcv_path = qff_ohlcv_path
        self.tsm_ohlcv_path = tsm_ohlcv_path
        self.usdttwd_ohlcv_path = usdttwd_ohlcv_path

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
        _ = expected  # Session-only PoC inputs are intentionally not continuous.

        qff_entry_open = None
        qff_entry_open_was_filled = None
        tsm_twd_fair_open = None
        if (
            self.qff_ohlcv_path is not None
            and self.tsm_ohlcv_path is not None
            and self.usdttwd_ohlcv_path is not None
        ):
            qff_open = read_open_series(self.qff_ohlcv_path, "qff").reindex(index)
            tsm_open = read_open_series(self.tsm_ohlcv_path, "tsm").reindex(index)
            usd_open = read_open_series(self.usdttwd_ohlcv_path, "usdttwd").reindex(index)
            if tsm_open.isna().any() or usd_open.isna().any():
                first_missing = tsm_open[tsm_open.isna()].index.union(
                    usd_open[usd_open.isna()].index
                )[0]
                raise RuntimeError(f"Replay entry open series missing at {first_missing}")
            qff_filled = pd.Series(
                pd.to_numeric(frame["qff_close_filled"], errors="coerce").to_numpy(),
                index=index,
            )
            qff_entry_open = qff_open.fillna(qff_filled)
            qff_entry_open_was_filled = qff_open.isna()
            tsm_twd_fair_open = tsm_open * usd_open / 5.0

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
                    qff_entry_price=(
                        float(qff_entry_open.iloc[row_index])
                        if qff_entry_open is not None
                        else None
                    ),
                    tsm_entry_twd_fair=(
                        float(tsm_twd_fair_open.iloc[row_index])
                        if tsm_twd_fair_open is not None
                        else None
                    ),
                    qff_was_filled=qff_close is None,
                    qff_entry_open_was_filled=(
                        bool(qff_entry_open_was_filled.iloc[row_index])
                        if qff_entry_open_was_filled is not None
                        else False
                    ),
                    expected_zscore=optional_float(row["spread_zscore"]),
                    expected_zscore_valid=parse_bool(row["zscore_valid"]),
                )
            )
        return self.calendar.annotate(bars)


def read_open_series(path: Path, name: str) -> pd.Series:
    if not path.exists():
        raise FileNotFoundError(f"Replay OHLCV CSV does not exist: {path}")
    frame = pd.read_csv(path)
    missing = {"timestamp", "open"}.difference(frame.columns)
    if missing:
        raise RuntimeError(f"{path} is missing required columns: {sorted(missing)}")
    timestamps = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(TAIPEI_TZ)
    values = pd.to_numeric(frame["open"], errors="coerce")
    if values.isna().any():
        raise RuntimeError(f"{path} has invalid open values for {name}")
    series = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(timestamps))
    if series.index.has_duplicates:
        raise RuntimeError(f"{path} has duplicate timestamps")
    return series.sort_index()
