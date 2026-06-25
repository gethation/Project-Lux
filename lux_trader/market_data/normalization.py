from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from ..core.contracts import row_get, row_to_dict
from ..core.time import TAIPEI_TZ, ensure_taipei
from .parsing import first_float, parse_timestamp


def close_series(frame: pd.DataFrame, name: str) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype=float, name=name)
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True).dt.tz_convert(
        TAIPEI_TZ
    )
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    if data["close"].isna().any():
        raise RuntimeError(f"{name} close series contains invalid values")
    series = pd.Series(
        data["close"].to_numpy(),
        index=pd.DatetimeIndex(data["timestamp"]),
        name=name,
    )
    return series.sort_index()


def normalize_ohlcv_rows(
    rows: list[list[float]],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "close"])
    frame = pd.DataFrame(
        rows,
        columns=["timestamp_ms", "open", "high", "low", "close", "volume"],
    )
    frame = frame.drop_duplicates("timestamp_ms", keep="last").sort_values(
        "timestamp_ms"
    )
    frame["timestamp"] = pd.to_datetime(
        frame["timestamp_ms"],
        unit="ms",
        utc=True,
    ).dt.tz_convert(TAIPEI_TZ)
    start_ts = pd.Timestamp(ensure_taipei(start))
    end_ts = pd.Timestamp(ensure_taipei(end))
    return frame.loc[
        (frame["timestamp"] >= start_ts) & (frame["timestamp"] <= end_ts),
        ["timestamp", "close"],
    ].copy()


def normalize_candle_rows(
    rows: list[Any],
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        raw = row_to_dict(row)
        timestamp = row_get(raw, "date", "time", "timestamp", "dateTime")
        close = first_float(raw, "close", "closePrice", "lastPrice", "price")
        if timestamp is None or close is None:
            continue
        normalized.append({"timestamp": parse_timestamp(timestamp), "close": close})
    frame = pd.DataFrame(normalized, columns=["timestamp", "close"])
    if frame.empty:
        return frame
    start_ts = pd.Timestamp(ensure_taipei(start))
    end_ts = pd.Timestamp(ensure_taipei(end))
    return frame.loc[
        (pd.DatetimeIndex(frame["timestamp"]) >= start_ts)
        & (pd.DatetimeIndex(frame["timestamp"]) <= end_ts)
    ].copy()

