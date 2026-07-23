from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from ..config import LiveMarketDataConfig
from ..core.models import MarketBar
from ..core.time import TAIPEI_TZ, ensure_taipei
from .normalization import close_series
from .parsing import parse_optional_float
from .session import (
    QFF_FORWARD_FILL_LOOKBACK,
    build_tw_leg_expected_warmup_index,
    floor_minute,
    prioritized_tw_leg_close_frame,
)
from .types import (
    OhlcvProvider,
    TwLegWarmupProvider,
    TwLegWarmupSourceReport,
)


class CsvTwLegWarmupProvider:
    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        if not self.path.exists():
            raise FileNotFoundError(f"TAIFEX QFF CSV does not exist: {self.path}")
        frame = pd.read_csv(self.path)
        required = {"timestamp", "close"}
        missing = required.difference(frame.columns)
        if missing:
            raise RuntimeError(f"{self.path} missing columns: {sorted(missing)}")
        frame = frame.copy()
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"],
            utc=True,
        ).dt.tz_convert(TAIPEI_TZ)
        frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
        start_ts = pd.Timestamp(ensure_taipei(start))
        end_ts = pd.Timestamp(ensure_taipei(end))
        return frame.loc[
            (frame["timestamp"] >= start_ts)
            & (frame["timestamp"] <= end_ts),
            ["timestamp", "close"],
        ].copy()


class WarmupBuilder:
    def __init__(
        self,
        *,
        live_config: LiveMarketDataConfig,
        tw_leg_intraday_provider: TwLegWarmupProvider | None,
        tw_leg_fallback_provider: TwLegWarmupProvider | None,
        us_leg_provider: OhlcvProvider,
        usdttwd_provider: OhlcvProvider,
        closed_dates: Iterable[date] = (),
    ) -> None:
        self.live_config = live_config
        self.tw_leg_intraday_provider = tw_leg_intraday_provider
        self.tw_leg_fallback_provider = tw_leg_fallback_provider
        self.us_leg_provider = us_leg_provider
        self.usdttwd_provider = usdttwd_provider
        self.closed_dates = tuple(closed_dates)

    def build(
        self,
        *,
        tw_leg_symbol: str,
        tw_leg_expiry: str | None = None,
        contract_policy_state: str | None = None,
        end: datetime | None = None,
    ) -> list[MarketBar]:
        end_minute = floor_minute(
            end or datetime.now(TAIPEI_TZ)
        ) - timedelta(minutes=1)
        tw_leg_fetch_start = end_minute - QFF_FORWARD_FILL_LOOKBACK

        # Prefer the live Fubon candle API.  TAIFEX/CSV is a true fallback:
        # do not pay its latency or let it block startup when Fubon alone can
        # produce a complete, quality-gated warmup window.
        tw_leg_parts: list[tuple[str, pd.DataFrame]] = []
        if self.tw_leg_intraday_provider is not None:
            tw_leg_parts.append(
                (
                    "intraday",
                    self.tw_leg_intraday_provider.fetch_1m(
                        tw_leg_symbol,
                        tw_leg_fetch_start,
                        end_minute,
                    ),
                )
            )
        fallback_loaded = False
        if not tw_leg_parts and self.tw_leg_fallback_provider is not None:
            tw_leg_parts.append(
                (
                    "fallback",
                    self.tw_leg_fallback_provider.fetch_1m(
                        tw_leg_symbol,
                        tw_leg_fetch_start,
                        end_minute,
                    ),
                )
            )
            fallback_loaded = True
        if not tw_leg_parts:
            raise RuntimeError("No QFF warmup providers configured")

        try:
            index, tw_leg_report = self._prepare_tw_leg_seed(
                tw_leg_parts,
                end_minute=end_minute,
                tw_leg_fetch_start=tw_leg_fetch_start,
            )
        except RuntimeError:
            if self.tw_leg_fallback_provider is None or fallback_loaded:
                raise
            tw_leg_parts.insert(
                0,
                (
                    "fallback",
                    self.tw_leg_fallback_provider.fetch_1m(
                        tw_leg_symbol,
                        tw_leg_fetch_start,
                        end_minute,
                    ),
                ),
            )
            index, tw_leg_report = self._prepare_tw_leg_seed(
                tw_leg_parts,
                end_minute=end_minute,
                tw_leg_fetch_start=tw_leg_fetch_start,
            )

        tw_leg = pd.Series(
            tw_leg_report.frame["merged_tw_leg_close"].to_numpy(),
            index=pd.DatetimeIndex(tw_leg_report.frame["timestamp"]),
        )
        tw_leg_filled = pd.Series(
            tw_leg_report.frame["tw_leg_close_filled"].to_numpy(),
            index=pd.DatetimeIndex(tw_leg_report.frame["timestamp"]),
        )
        start_minute = index[0].to_pydatetime()
        last_timestamp = index[-1].to_pydatetime()
        us_leg = close_series(
            self.us_leg_provider.fetch_ohlcv_1m(
                self.live_config.binance_symbol,
                start_minute,
                last_timestamp,
            ),
            "us_leg",
        ).reindex(index)
        usd = close_series(
            self.usdttwd_provider.fetch_ohlcv_1m(
                self.live_config.bitopro_symbol,
                start_minute,
                last_timestamp,
            ),
            "usdttwd",
        ).reindex(index)
        missing = us_leg[us_leg.isna()].index.union(usd[usd.isna()].index)
        if len(missing):
            raise RuntimeError(
                f"TSM/USDT-TWD warmup has missing minutes from {missing[0]}"
            )

        us_leg_twd_fair = us_leg * usd / 5.0
        spread = (
            (us_leg_twd_fair - tw_leg_filled)
            / (us_leg_twd_fair + tw_leg_filled)
            * 200.0
        )
        bars: list[MarketBar] = []
        for row_index, timestamp in enumerate(index):
            tw_leg_close = parse_optional_float(tw_leg.loc[timestamp])
            bars.append(
                MarketBar(
                    row_index=row_index - len(index),
                    timestamp=timestamp.to_pydatetime(),
                    tw_leg_close=tw_leg_close,
                    tw_leg_close_filled=float(tw_leg_filled.loc[timestamp]),
                    us_leg_twd_fair=float(us_leg_twd_fair.loc[timestamp]),
                    spread=float(spread.loc[timestamp]),
                    tw_leg_was_filled=tw_leg_close is None,
                    tw_leg_symbol=tw_leg_symbol,
                    tw_leg_expiry=tw_leg_expiry,
                    contract_policy_state=contract_policy_state,
                )
            )
        return bars

    def _prepare_tw_leg_seed(
        self,
        tw_leg_parts: list[tuple[str, pd.DataFrame]],
        *,
        end_minute: datetime,
        tw_leg_fetch_start: datetime,
    ) -> tuple[pd.DatetimeIndex, TwLegWarmupSourceReport]:
        index, session_index = build_tw_leg_expected_warmup_index(
            start=tw_leg_fetch_start,
            end=end_minute,
            count=self.live_config.warmup_minutes,
            closed_dates=self.closed_dates,
        )
        start_minute = index[0].to_pydatetime()
        tw_leg_report = build_tw_leg_warmup_source_report(
            tw_leg_parts,
            start_minute=start_minute,
            end_minute=end_minute,
            tw_leg_fetch_start=tw_leg_fetch_start,
            warmup_index=index,
            fill_index=session_index,
        )
        validate_tw_leg_warmup_report(
            tw_leg_report,
            max_trailing_fill_minutes=(
                self.live_config.warmup_tw_leg_max_trailing_fill_minutes
            ),
            max_forward_fill_ratio=self.live_config.warmup_forward_fill_max_ratio,
        )
        return index, tw_leg_report


def validate_tw_leg_warmup_report(
    tw_leg_report: TwLegWarmupSourceReport,
    *,
    max_trailing_fill_minutes: int,
    max_forward_fill_ratio: float,
) -> None:
    """Fail closed when an expected QFF warmup window is missing or stale."""
    if tw_leg_report.null_count:
        first_missing = tw_leg_report.frame.loc[
            tw_leg_report.frame["tw_leg_close_filled"].isna(),
            "timestamp",
        ].iloc[0]
        raise RuntimeError(f"QFF warmup cannot forward-fill from {first_missing}")

    actual_tw_leg = tw_leg_report.frame["merged_tw_leg_close"].notna()
    if not actual_tw_leg.any():
        raise RuntimeError(
            "QFF warmup latest actual bar is stale: "
            "no actual QFF bar exists in the expected warmup window"
        )
    last_actual_position = int(actual_tw_leg[actual_tw_leg].index[-1])
    trailing_filled = len(tw_leg_report.frame) - last_actual_position - 1
    if trailing_filled > max_trailing_fill_minutes:
        last_actual_timestamp = tw_leg_report.frame.loc[
            last_actual_position,
            "timestamp",
        ]
        expected_end = tw_leg_report.frame.iloc[-1]["timestamp"]
        raise RuntimeError(
            "QFF warmup latest actual bar is stale: "
            f"last={last_actual_timestamp}, expected_end={expected_end}, "
            f"trailing_fill={trailing_filled} minutes exceeds max "
            f"{max_trailing_fill_minutes}"
        )

    # Data-quality gate: too many forward-filled QFF minutes means the feed
    # was mostly dead during warmup, so the rolling z-score is unreliable.
    total_minutes = len(tw_leg_report.frame)
    forward_filled = int(tw_leg_report.source_used_counts.get("forward_fill", 0))
    if total_minutes and max_forward_fill_ratio < 1.0:
        forward_fill_ratio = forward_filled / total_minutes
        if forward_fill_ratio > max_forward_fill_ratio:
            raise RuntimeError(
                "QFF warmup forward-fill ratio "
                f"{forward_fill_ratio:.3f} exceeds max "
                f"{max_forward_fill_ratio:.3f} "
                f"({forward_filled}/{total_minutes} minutes); refusing to "
                "seed the indicator on degraded warmup data"
            )


def build_tw_leg_warmup_source_report(
    frames: list[tuple[str, pd.DataFrame]],
    *,
    start_minute: datetime,
    end_minute: datetime,
    tw_leg_fetch_start: datetime,
    warmup_index: pd.DatetimeIndex | None = None,
    fill_index: pd.DatetimeIndex | None = None,
) -> TwLegWarmupSourceReport:
    start_minute = floor_minute(start_minute)
    end_minute = floor_minute(end_minute)
    tw_leg_fetch_start = floor_minute(tw_leg_fetch_start)
    if warmup_index is None:
        warmup_index = pd.date_range(start_minute, end_minute, freq="min")
    else:
        warmup_index = pd.DatetimeIndex(warmup_index)
    if fill_index is None:
        fill_index = pd.date_range(tw_leg_fetch_start, end_minute, freq="min")
    else:
        fill_index = pd.DatetimeIndex(fill_index)

    source_series = {
        source: close_series(frame, source)
        for source, frame in frames
    }
    combined = prioritized_tw_leg_close_frame(frames)

    report = pd.DataFrame({"timestamp": warmup_index})
    for source, series in source_series.items():
        report[f"{source}_close"] = series.reindex(warmup_index).to_numpy()
    report["merged_tw_leg_close"] = combined["close"].reindex(warmup_index).to_numpy()
    filled = combined["close"].reindex(fill_index).ffill().reindex(warmup_index)
    direct_source = combined["source"].reindex(warmup_index)
    report["tw_leg_close_filled"] = filled.to_numpy()
    report["source_used"] = direct_source.where(
        direct_source.notna(),
        other=pd.Series("forward_fill", index=warmup_index).where(filled.notna()),
    ).to_numpy()

    overlap_rows = 0
    mismatch_count = 0
    max_abs_diff = 0.0
    if "taifex" in source_series and "fubon" in source_series:
        overlap = pd.DataFrame(
            {
                "taifex": source_series["taifex"],
                "fubon": source_series["fubon"],
            }
        ).dropna()
        overlap_rows = len(overlap)
        if overlap_rows:
            diffs = (overlap["taifex"] - overlap["fubon"]).abs()
            mismatches = diffs[diffs > 1e-9]
            mismatch_count = len(mismatches)
            max_abs_diff = (
                float(mismatches.max())
                if mismatch_count
                else 0.0
            )

    return TwLegWarmupSourceReport(
        frame=report,
        start=start_minute,
        end=end_minute,
        tw_leg_fetch_start=tw_leg_fetch_start,
        source_rows={
            source: len(series)
            for source, series in source_series.items()
        },
        source_used_counts={
            str(key): int(value)
            for key, value in report["source_used"].value_counts(
                dropna=False
            ).items()
        },
        null_count=int(report["tw_leg_close_filled"].isna().sum()),
        overlap_rows=overlap_rows,
        mismatch_count=mismatch_count,
        max_abs_diff=max_abs_diff,
    )

