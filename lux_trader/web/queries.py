"""Read-only queries that turn a store into chart-ready JSON.

Two rules shape everything here:

1. **The chart may not disagree with the engine.** A candle's CLOSE is always
   ``bars.spread`` -- the number the strategy actually scored and traded on --
   never something recomputed from ticks. Only open/high/low come from the tick
   reconstruction, because the store has no other record of what happened
   *inside* a minute (schema.py:74 keeps one spread per bar).

2. **Reuse the frozen maths.** ``spread_from_prices`` and
   ``umc_contract_twd_price`` are imported from core rather than reimplemented,
   so a change to the spread definition cannot silently desync the picture from
   the position.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from ..core.sizing import umc_contract_twd_price
from ..core.tradable_spread import spread_from_prices

# The engine builds UMC's TWD fair value by dividing by five, hardcoded, in both
# places that matter (minute_bar.py:189, tradable_spread.py:148). It is NOT read
# from fees.umc_contract_multiplier there, so the reconstruction copies the
# engine rather than the config -- matching the engine is what keeps the candle
# close and the reconstructed open on the same scale.
UMC_TWD_FAIR_DIVISOR = 5.0

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
}

BAR_COLUMNS = (
    "row_index, timestamp, spread, spread_mean, spread_std, spread_zscore,"
    " zscore_valid, short_spread, short_zscore, long_spread, long_zscore,"
    " decision_spread_type, decision_zscore, state, position, umc_units,"
    " ccf_units, ccf_contracts, realized_pnl, unrealized_pnl, equity,"
    " umc_twd_fair, ccf_close_filled, ccf_symbol, entry_allowed, close_allowed"
)


def read_only_uri(path: Path) -> str:
    return f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def display_epoch(moment: datetime) -> int:
    """Seconds for lightweight-charts, shifted so the axis reads local time.

    The library formats a UTCTimestamp in UTC. Every timestamp in this store is
    stamped +08:00, so adding the offset makes the axis show Taipei wall clock
    -- which is the only clock anyone operating this system thinks in. The API
    reports the shift in ``tz_offset_seconds`` so nothing has to guess.
    """
    offset = moment.utcoffset()
    shift = int(offset.total_seconds()) if offset is not None else 0
    return int(moment.timestamp()) + shift


def tz_offset_seconds(rows: Sequence[Any], key: str = "timestamp") -> int:
    for row in rows:
        offset = parse_ts(row[key]).utcoffset()
        if offset is not None:
            return int(offset.total_seconds())
    return 0


def bucket_start(epoch: int, seconds: int) -> int:
    return epoch - (epoch % seconds)


def as_float(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass(frozen=True)
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float

    def to_json(self) -> dict[str, Any]:
        # A candle whose high is not the highest confuses the renderer and, worse,
        # the reader. The close is authoritative, so widen the wick to it.
        high = max(self.open, self.high, self.low, self.close)
        low = min(self.open, self.high, self.low, self.close)
        return {
            "time": self.time,
            "open": round(self.open, 6),
            "high": round(high, 6),
            "low": round(low, 6),
            "close": round(self.close, 6),
        }


class StoreReader:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(read_only_uri(self.path), uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    # ------------------------------------------------------------------
    # Raw fetches
    # ------------------------------------------------------------------
    def latest_bars(self, limit: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT {BAR_COLUMNS} FROM bars ORDER BY row_index DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return list(reversed(rows))

    def has_ticks_since(self, timestamp: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM market_ticks WHERE observed_at >= ? LIMIT 1",
                (timestamp,),
            ).fetchone()
        return row is not None

    def ticks_since(self, timestamp: str, fx_symbol: str) -> list[sqlite3.Row]:
        """One row per poll, with all three legs pivoted onto it.

        The engine writes the three quotes of an iteration under a single
        ``observed_at`` (engine.py:484), so grouping on it recovers the exact
        triple the loop saw -- no nearest-neighbour matching, no interpolation.
        """
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT observed_at,
                       MAX(CASE WHEN source LIKE '%ccf%' THEN price END) AS ccf,
                       MAX(CASE WHEN source LIKE '%ccf%' THEN bid   END) AS ccf_bid,
                       MAX(CASE WHEN source LIKE '%ccf%' THEN ask   END) AS ccf_ask,
                       MAX(CASE WHEN source LIKE '%umc%' THEN price END) AS umc,
                       MAX(CASE WHEN source LIKE '%umc%' THEN bid   END) AS umc_bid,
                       MAX(CASE WHEN source LIKE '%umc%' THEN ask   END) AS umc_ask,
                       MAX(CASE WHEN symbol = ?          THEN price END) AS fx
                FROM market_ticks
                WHERE observed_at >= ?
                GROUP BY observed_at
                ORDER BY observed_at
                """,
                (fx_symbol, timestamp),
            ).fetchall()

    def latest_tick(self, fx_symbol: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(observed_at) AS observed_at FROM market_ticks"
            ).fetchone()
            if row is None or row["observed_at"] is None:
                return None
            ticks = self.ticks_since(str(row["observed_at"]), fx_symbol)
        return dict(ticks[0]) if ticks else None

    def strategy_state(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT row_index, timestamp, state_json FROM strategy_state WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["state_json"])
        payload["_row_index"] = int(row["row_index"])
        payload["_timestamp"] = str(row["timestamp"])
        return payload

    def trades(self, limit: int = 500) -> list[sqlite3.Row]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, direction, entry_signal_time, entry_signal_zscore,
                       entry_time, entry_fill_zscore, entry_delay_minutes,
                       exit_signal_time, exit_signal_zscore, exit_time,
                       exit_fill_zscore, exit_reason, net_pnl_twd, total_fee_twd,
                       holding_minutes, ccf_contracts, umc_units
                FROM trades ORDER BY trade_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return list(reversed(rows))

    def store_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            def count(table: str) -> int:
                try:
                    return int(
                        connection.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
                    )
                except sqlite3.Error:
                    return 0

            span = connection.execute(
                "SELECT MIN(timestamp) a, MAX(timestamp) b FROM bars"
            ).fetchone()
            run = connection.execute(
                "SELECT mode, started_at, finished_at, status FROM live_runs"
                " ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
        return {
            "bars": count("bars"),
            "trades": count("trades"),
            "market_ticks": count("market_ticks"),
            "first_bar": span["a"] if span else None,
            "last_bar": span["b"] if span else None,
            "latest_run": dict(run) if run is not None else None,
        }


# ----------------------------------------------------------------------
# Reconstruction
# ----------------------------------------------------------------------
def tick_spreads(rows: Iterable[sqlite3.Row]) -> list[tuple[str, float]]:
    """(observed_at, mid spread) for every poll that had all three legs."""
    out: list[tuple[str, float]] = []
    for row in rows:
        ccf = as_float(row["ccf"])
        umc = as_float(row["umc"])
        fx = as_float(row["fx"])
        if ccf is None or umc is None or fx is None or ccf <= 0:
            continue
        umc_twd_fair = umc * fx / UMC_TWD_FAIR_DIVISOR
        out.append((str(row["observed_at"]), spread_from_prices(umc_twd_fair, ccf)))
    return out


def minute_candles(
    bars: Sequence[sqlite3.Row],
    ticks: Sequence[tuple[str, float]],
) -> tuple[list[Candle], bool]:
    """1m candles: open/high/low from ticks, close from ``bars.spread``.

    Minutes with no bar row are dropped on purpose. The engine rejects a minute
    it could not build (stale data, leg skew), and drawing a candle there would
    show the operator a price the strategy never had.
    """
    intra: dict[str, list[float]] = {}
    for observed_at, spread in ticks:
        intra.setdefault(observed_at[:16], []).append(spread)

    candles: list[Candle] = []
    reconstructed = False
    for bar in bars:
        timestamp = str(bar["timestamp"])
        close = float(bar["spread"])
        values = intra.get(timestamp[:16])
        if values:
            reconstructed = True
            candles.append(
                Candle(
                    time=display_epoch(parse_ts(timestamp)),
                    open=values[0],
                    high=max(values),
                    low=min(values),
                    close=close,
                )
            )
        else:
            candles.append(
                Candle(
                    time=display_epoch(parse_ts(timestamp)),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                )
            )
    return candles, reconstructed


def aggregate_candles(candles: Sequence[Candle], seconds: int) -> list[Candle]:
    if seconds <= 60:
        return list(candles)
    buckets: dict[int, Candle] = {}
    order: list[int] = []
    for candle in candles:
        key = bucket_start(candle.time, seconds)
        current = buckets.get(key)
        if current is None:
            order.append(key)
            buckets[key] = Candle(key, candle.open, candle.high, candle.low, candle.close)
            continue
        buckets[key] = Candle(
            time=key,
            open=current.open,
            high=max(current.high, candle.high),
            low=min(current.low, candle.low),
            close=candle.close,
        )
    return [buckets[key] for key in order]


def bucket_last(
    bars: Sequence[sqlite3.Row],
    seconds: int,
    fields: Sequence[str],
) -> list[dict[str, Any]]:
    """Last observed value of each field per bucket -- matching the candle close."""
    buckets: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for bar in bars:
        key = bucket_start(display_epoch(parse_ts(str(bar["timestamp"]))), seconds)
        if key not in buckets:
            order.append(key)
            buckets[key] = {"time": key}
        entry = buckets[key]
        for field_name in fields:
            entry[field_name] = as_float(bar[field_name])
    return [buckets[key] for key in order]


def threshold_series(
    stats: Sequence[dict[str, Any]],
    entry_z: float,
    exit_z: float,
) -> list[dict[str, Any]]:
    """The z thresholds expressed in spread units.

    They move, because the mean and std are rolling (indicator.py:31-37). That
    movement is the point: a flat line drawn at a fixed spread would be a
    threshold this strategy does not have.
    """
    out: list[dict[str, Any]] = []
    for row in stats:
        mean = row.get("spread_mean")
        std = row.get("spread_std")
        item: dict[str, Any] = {"time": row["time"]}
        if mean is not None and std is not None:
            item["mean"] = mean
            item["entry_upper"] = mean + entry_z * std
            item["entry_lower"] = mean - entry_z * std
            item["exit_upper"] = mean + exit_z * std
            item["exit_lower"] = mean - exit_z * std
        out.append(item)
    return out
