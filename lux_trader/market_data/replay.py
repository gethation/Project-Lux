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


# The PoC owns the replay CSV's column names, and it emits the same qff_/tsm_
# headers for BOTH pairs -- the CCF/UMC pipeline
# (scripts/calculate_ccf_umc_spread_1m.py) reuses them verbatim. They are an
# external contract, not our naming, so they stay put while every identifier
# on this side of the boundary is ccf_/umc_. Renaming them here would only
# break the ability to read the PoC's own output.
CSV_CCF_CLOSE = "qff_close"
CSV_CCF_CLOSE_FILLED = "qff_close_filled"
CSV_UMC_TWD_FAIR = "tsm_twd_fair"


class CsvReplayMarketData:
    def __init__(
        self,
        csv_path: Path,
        calendar: TradingCalendar | None = None,
        *,
        ccf_ohlcv_path: Path | None = None,
        umc_ohlcv_path: Path | None = None,
        usdttwd_ohlcv_path: Path | None = None,
    ) -> None:
        self.csv_path = csv_path
        self.calendar = calendar or TradingCalendar()
        self.ccf_ohlcv_path = ccf_ohlcv_path
        self.umc_ohlcv_path = umc_ohlcv_path
        self.usdttwd_ohlcv_path = usdttwd_ohlcv_path

    def load(self) -> list[MarketBar]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Input CSV does not exist: {self.csv_path}")

        frame = pd.read_csv(self.csv_path)
        required = {
            "timestamp",
            CSV_CCF_CLOSE,
            CSV_CCF_CLOSE_FILLED,
            CSV_UMC_TWD_FAIR,
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

        ccf_entry_open = None
        ccf_entry_open_was_filled = None
        umc_twd_fair_open = None
        if (
            self.ccf_ohlcv_path is not None
            and self.umc_ohlcv_path is not None
            and self.usdttwd_ohlcv_path is not None
        ):
            ccf_open = read_open_series(self.ccf_ohlcv_path, "ccf").reindex(index)
            umc_open = read_open_series(self.umc_ohlcv_path, "umc").reindex(index)
            usd_open = read_open_series(self.usdttwd_ohlcv_path, "usdttwd").reindex(index)
            if umc_open.isna().any() or usd_open.isna().any():
                first_missing = umc_open[umc_open.isna()].index.union(
                    usd_open[usd_open.isna()].index
                )[0]
                raise RuntimeError(f"Replay entry open series missing at {first_missing}")
            ccf_filled = pd.Series(
                pd.to_numeric(frame[CSV_CCF_CLOSE_FILLED], errors="coerce").to_numpy(),
                index=index,
            )
            ccf_entry_open = ccf_open.fillna(ccf_filled)
            ccf_entry_open_was_filled = ccf_open.isna()
            umc_twd_fair_open = umc_open * usd_open / 5.0

        bars: list[MarketBar] = []
        for row_index, row in frame.iterrows():
            ccf_close = optional_float(row[CSV_CCF_CLOSE])
            ccf_close_filled = optional_float(row[CSV_CCF_CLOSE_FILLED])
            umc_twd_fair = optional_float(row[CSV_UMC_TWD_FAIR])
            spread = optional_float(row["spread"])
            if ccf_close_filled is None or umc_twd_fair is None or spread is None:
                raise RuntimeError(f"Invalid market data at row {row_index}")
            bars.append(
                MarketBar(
                    row_index=int(row_index),
                    timestamp=timestamps.iloc[row_index].to_pydatetime(),
                    ccf_close=ccf_close,
                    ccf_close_filled=ccf_close_filled,
                    umc_twd_fair=umc_twd_fair,
                    spread=spread,
                    ccf_entry_price=(
                        float(ccf_entry_open.iloc[row_index])
                        if ccf_entry_open is not None
                        else None
                    ),
                    umc_entry_twd_fair=(
                        float(umc_twd_fair_open.iloc[row_index])
                        if umc_twd_fair_open is not None
                        else None
                    ),
                    ccf_was_filled=ccf_close is None,
                    ccf_entry_open_was_filled=(
                        bool(ccf_entry_open_was_filled.iloc[row_index])
                        if ccf_entry_open_was_filled is not None
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
