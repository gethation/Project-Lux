from __future__ import annotations

from collections.abc import Iterable
import re
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from ..core.calendar import (
    in_day_session,
    in_night_session,
    is_live_business_day,
    session_start_date,
)
from ..core.contracts import parse_contract_expiry, row_get, row_to_dict
from ..core.time import TAIPEI_TZ, ensure_taipei
from .normalization import close_series
from .parsing import parse_optional_float, parse_timestamp
from .types import TwLegContractCandidate


QFF_FORWARD_FILL_LOOKBACK = timedelta(days=14)
QFF_MONTH_CODES = {
    "A": 1,
    "B": 2,
    "C": 3,
    "D": 4,
    "E": 5,
    "F": 6,
    "G": 7,
    "H": 8,
    "I": 9,
    "J": 10,
    "K": 11,
    "L": 12,
}


def floor_minute(timestamp: datetime) -> datetime:
    return ensure_taipei(timestamp).replace(second=0, microsecond=0)


def market_time(hour: int, minute: int) -> datetime.time:
    return datetime.min.replace(hour=hour, minute=minute).time()


def build_tw_leg_session_index(
    tw_leg_close: pd.Series,
    *,
    end: datetime | None = None,
) -> pd.DatetimeIndex:
    tw_leg_close = tw_leg_close.dropna().sort_index()
    if tw_leg_close.empty:
        return pd.DatetimeIndex([], tz=TAIPEI_TZ)
    end_ts = (
        pd.Timestamp(ensure_taipei(end))
        if end is not None
        else tw_leg_close.index.max()
    )
    tw_leg_close = tw_leg_close.loc[tw_leg_close.index <= end_ts]
    if tw_leg_close.empty:
        return pd.DatetimeIndex([], tz=TAIPEI_TZ)

    pieces: list[pd.DatetimeIndex] = []
    first_timestamp = tw_leg_close.index.min().to_pydatetime()
    first_day = min(first_timestamp.date(), session_start_date(first_timestamp))
    last_day = end_ts.date()
    for day in pd.date_range(first_day, last_day, freq="D", tz=TAIPEI_TZ):
        day_date = day.date()
        day_mask = [
            ts.date() == day_date and in_day_session(ts.to_pydatetime())
            for ts in tw_leg_close.index
        ]
        if tw_leg_close.loc[day_mask].notna().any():
            pieces.append(
                pd.date_range(
                    datetime.combine(
                        day_date,
                        market_time(8, 45),
                        tzinfo=TAIPEI_TZ,
                    ),
                    datetime.combine(
                        day_date,
                        market_time(13, 45),
                        tzinfo=TAIPEI_TZ,
                    ),
                    freq="min",
                )
            )

        night_mask = [
            session_start_date(ts.to_pydatetime()) == day_date
            and in_night_session(ts.to_pydatetime())
            for ts in tw_leg_close.index
        ]
        if tw_leg_close.loc[night_mask].notna().any():
            pieces.append(
                pd.date_range(
                    datetime.combine(
                        day_date,
                        market_time(17, 25),
                        tzinfo=TAIPEI_TZ,
                    ),
                    datetime.combine(
                        day_date + timedelta(days=1),
                        market_time(5, 0),
                        tzinfo=TAIPEI_TZ,
                    ),
                    freq="min",
                )
            )

    if not pieces:
        return pd.DatetimeIndex([], tz=TAIPEI_TZ)
    index = pieces[0].append(pieces[1:]).unique().sort_values()
    index = index[(index >= tw_leg_close.index.min()) & (index <= end_ts)]
    return pd.DatetimeIndex(index)


def build_tw_leg_session_warmup_index(
    tw_leg_close: pd.Series,
    *,
    end: datetime,
    count: int,
) -> pd.DatetimeIndex:
    session_index = build_tw_leg_session_index(tw_leg_close, end=end)
    if len(session_index) < count:
        raise RuntimeError(
            f"QFF session warmup has only {len(session_index)} bars, need {count}"
        )
    return pd.DatetimeIndex(session_index[-count:])


def build_tw_leg_expected_session_index(
    *,
    start: datetime,
    end: datetime,
    closed_dates: Iterable[date] = (),
) -> pd.DatetimeIndex:
    """Return every QFF trading minute required by the live calendar.

    Unlike :func:`build_tw_leg_session_index`, this index is anchored to the
    requested time range rather than inferred from whatever rows a provider
    happened to return.  A wholly missing current day/night session therefore
    remains visible to warmup freshness checks instead of disappearing.
    """
    start_ts = pd.Timestamp(floor_minute(start))
    end_ts = pd.Timestamp(floor_minute(end))
    if end_ts < start_ts:
        return pd.DatetimeIndex([], tz=TAIPEI_TZ)

    closed = set(closed_dates)
    candidates = pd.date_range(start_ts, end_ts, freq="min")
    expected: list[pd.Timestamp] = []
    for timestamp in candidates:
        value = timestamp.to_pydatetime()
        if value.date() in closed:
            continue
        if in_day_session(value):
            trading_date = value.date()
        elif in_night_session(value):
            trading_date = session_start_date(value)
        else:
            continue
        if is_live_business_day(trading_date, closed):
            expected.append(timestamp)
    return pd.DatetimeIndex(expected)


def build_tw_leg_expected_warmup_index(
    *,
    start: datetime,
    end: datetime,
    count: int,
    closed_dates: Iterable[date] = (),
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """Return ``(last_count_minutes, full_fill_index)`` for live warmup."""
    session_index = build_tw_leg_expected_session_index(
        start=start,
        end=end,
        closed_dates=closed_dates,
    )
    if len(session_index) < count:
        raise RuntimeError(
            "QFF expected-session warmup has only "
            f"{len(session_index)} bars, need {count}"
        )
    return pd.DatetimeIndex(session_index[-count:]), session_index


def prioritized_tw_leg_close_frame(
    frames: list[tuple[str, pd.DataFrame]],
) -> pd.DataFrame:
    combined_parts: list[pd.DataFrame] = []
    for priority, (source, frame) in enumerate(frames):
        series = close_series(frame, source)
        if series.empty:
            continue
        combined_parts.append(
            pd.DataFrame(
                {
                    "timestamp": series.index,
                    "close": series.to_numpy(),
                    "source": source,
                    "priority": priority,
                }
            )
        )
    if not combined_parts:
        output = pd.DataFrame(columns=["close", "source", "priority"])
        output.index = pd.DatetimeIndex([], tz=TAIPEI_TZ)
        return output
    combined = pd.concat(combined_parts, ignore_index=True).sort_values(
        ["timestamp", "priority"]
    )
    return combined.drop_duplicates("timestamp", keep="last").set_index("timestamp")


def tw_leg_symbol_to_taifex_contract_month(
    symbol: str,
    *,
    reference_date: date | None = None,
) -> str:
    normalized = symbol.strip().upper()
    numeric = re.search(r"(20\d{2})(0[1-9]|1[0-2])", normalized)
    if numeric:
        return f"{numeric.group(1)}{numeric.group(2)}"

    coded = re.search(r"QFF([A-L])(\d)", normalized)
    if coded is None:
        raise RuntimeError(
            f"Cannot derive TAIFEX contract month from QFF symbol: {symbol}"
        )

    reference = reference_date or datetime.now(TAIPEI_TZ).date()
    year_digit = int(coded.group(2))
    decade = reference.year - reference.year % 10
    year = decade + year_digit
    while year < reference.year - 1:
        year += 10
    month = QFF_MONTH_CODES[coded.group(1)]
    return f"{year}{month:02d}"


def select_tw_leg_front_month(
    candidates: list[Any],
    *,
    product: str = "QFF",
    today: date | None = None,
) -> TwLegContractCandidate:
    today = today or datetime.now(TAIPEI_TZ).date()
    parsed: list[TwLegContractCandidate] = []
    rejected: list[str] = []

    for row in candidates:
        raw = row_to_dict(row)
        symbol = str(row_get(raw, "symbol", "code", "id", "ticker") or "").strip()
        if not symbol:
            rejected.append(str(raw))
            continue
        product_value = str(
            row_get(raw, "product", "productCode", "name") or symbol
        )
        if (
            product.upper() not in product_value.upper()
            and product.upper() not in symbol.upper()
        ):
            continue
        expiry = parse_contract_expiry(raw, product)
        if expiry is None:
            rejected.append(symbol)
            continue
        if expiry >= today:
            parsed.append(
                TwLegContractCandidate(symbol=symbol, expiry=expiry, raw=raw)
            )

    if not parsed:
        raise RuntimeError(
            "Unable to select QFF front-month contract. "
            f"Rejected candidates: {rejected[:10]}"
        )
    return sorted(parsed, key=lambda item: (item.expiry, item.symbol))[0]


__all__ = [
    "QFF_FORWARD_FILL_LOOKBACK",
    "build_tw_leg_session_index",
    "build_tw_leg_session_warmup_index",
    "build_tw_leg_expected_session_index",
    "build_tw_leg_expected_warmup_index",
    "floor_minute",
    "parse_optional_float",
    "parse_timestamp",
    "prioritized_tw_leg_close_frame",
    "tw_leg_symbol_to_taifex_contract_month",
    "select_tw_leg_front_month",
]

