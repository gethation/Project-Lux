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
        tw_leg_ohlcv_path: Path | None = None,
        us_leg_ohlcv_path: Path | None = None,
        usdttwd_ohlcv_path: Path | None = None,
    ) -> None:
        self.csv_path = csv_path
        self.calendar = calendar or TradingCalendar()
        self.tw_leg_ohlcv_path = tw_leg_ohlcv_path
        self.us_leg_ohlcv_path = us_leg_ohlcv_path
        self.usdttwd_ohlcv_path = usdttwd_ohlcv_path

    def load(self) -> list[MarketBar]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Input CSV does not exist: {self.csv_path}")

        frame = pd.read_csv(self.csv_path)
        required = {
            "timestamp",
            "tw_leg_close",
            "tw_leg_close_filled",
            "us_leg_twd_fair",
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

        tw_leg_entry_open = None
        tw_leg_entry_open_was_filled = None
        us_leg_twd_fair_open = None
        if (
            self.tw_leg_ohlcv_path is not None
            and self.us_leg_ohlcv_path is not None
            and self.usdttwd_ohlcv_path is not None
        ):
            tw_leg_open = read_open_series(self.tw_leg_ohlcv_path, "tw_leg").reindex(index)
            us_leg_open = read_open_series(self.us_leg_ohlcv_path, "us_leg").reindex(index)
            usd_open = read_open_series(self.usdttwd_ohlcv_path, "usdttwd").reindex(index)
            if us_leg_open.isna().any() or usd_open.isna().any():
                first_missing = us_leg_open[us_leg_open.isna()].index.union(
                    usd_open[usd_open.isna()].index
                )[0]
                raise RuntimeError(f"Replay entry open series missing at {first_missing}")
            tw_leg_filled = pd.Series(
                pd.to_numeric(frame["tw_leg_close_filled"], errors="coerce").to_numpy(),
                index=index,
            )
            tw_leg_entry_open = tw_leg_open.fillna(tw_leg_filled)
            tw_leg_entry_open_was_filled = tw_leg_open.isna()
            us_leg_twd_fair_open = us_leg_open * usd_open / 5.0

        bars: list[MarketBar] = []
        for row_index, row in frame.iterrows():
            tw_leg_close = optional_float(row["tw_leg_close"])
            tw_leg_close_filled = optional_float(row["tw_leg_close_filled"])
            us_leg_twd_fair = optional_float(row["us_leg_twd_fair"])
            spread = optional_float(row["spread"])
            if tw_leg_close_filled is None or us_leg_twd_fair is None or spread is None:
                raise RuntimeError(f"Invalid market data at row {row_index}")
            bars.append(
                MarketBar(
                    row_index=int(row_index),
                    timestamp=timestamps.iloc[row_index].to_pydatetime(),
                    tw_leg_close=tw_leg_close,
                    tw_leg_close_filled=tw_leg_close_filled,
                    us_leg_twd_fair=us_leg_twd_fair,
                    spread=spread,
                    tw_leg_entry_price=(
                        float(tw_leg_entry_open.iloc[row_index])
                        if tw_leg_entry_open is not None
                        else None
                    ),
                    us_leg_entry_twd_fair=(
                        float(us_leg_twd_fair_open.iloc[row_index])
                        if us_leg_twd_fair_open is not None
                        else None
                    ),
                    tw_leg_was_filled=tw_leg_close is None,
                    tw_leg_entry_open_was_filled=(
                        bool(tw_leg_entry_open_was_filled.iloc[row_index])
                        if tw_leg_entry_open_was_filled is not None
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
