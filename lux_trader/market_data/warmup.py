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
    CCF_FORWARD_FILL_LOOKBACK,
    build_ccf_expected_warmup_index,
    floor_minute,
    prioritized_ccf_close_frame,
)
from .types import (
    OhlcvProvider,
    CcfWarmupProvider,
    CcfWarmupSourceReport,
)


class CsvCcfWarmupProvider:
    def __init__(self, path: Path) -> None:
        self.path = path

    def fetch_1m(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        if not self.path.exists():
            raise FileNotFoundError(f"TAIFEX CCF CSV does not exist: {self.path}")
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
        ccf_intraday_provider: CcfWarmupProvider | None,
        ccf_fallback_provider: CcfWarmupProvider | None,
        umc_provider: OhlcvProvider,
        usd_twd_provider: OhlcvProvider,
        closed_dates: Iterable[date] = (),
    ) -> None:
        self.live_config = live_config
        self.ccf_intraday_provider = ccf_intraday_provider
        self.ccf_fallback_provider = ccf_fallback_provider
        self.umc_provider = umc_provider
        self.usd_twd_provider = usd_twd_provider
        self.closed_dates = tuple(closed_dates)

    def build(
        self,
        *,
        ccf_symbol: str,
        ccf_expiry: str | None = None,
        contract_policy_state: str | None = None,
        end: datetime | None = None,
    ) -> list[MarketBar]:
        end_minute = floor_minute(
            end or datetime.now(TAIPEI_TZ)
        ) - timedelta(minutes=1)
        ccf_fetch_start = end_minute - CCF_FORWARD_FILL_LOOKBACK

        # Prefer the live Fubon candle API.  TAIFEX/CSV is a true fallback:
        # do not pay its latency or let it block startup when Fubon alone can
        # produce a complete, quality-gated warmup window.
        ccf_parts: list[tuple[str, pd.DataFrame]] = []
        if self.ccf_intraday_provider is not None:
            ccf_parts.append(
                (
                    "intraday",
                    self.ccf_intraday_provider.fetch_1m(
                        ccf_symbol,
                        ccf_fetch_start,
                        end_minute,
                    ),
                )
            )
        fallback_loaded = False
        if not ccf_parts and self.ccf_fallback_provider is not None:
            ccf_parts.append(
                (
                    "fallback",
                    self.ccf_fallback_provider.fetch_1m(
                        ccf_symbol,
                        ccf_fetch_start,
                        end_minute,
                    ),
                )
            )
            fallback_loaded = True
        if not ccf_parts:
            raise RuntimeError("No CCF warmup providers configured")

        try:
            index, ccf_report = self._prepare_ccf_seed(
                ccf_parts,
                end_minute=end_minute,
                ccf_fetch_start=ccf_fetch_start,
            )
        except RuntimeError:
            if self.ccf_fallback_provider is None or fallback_loaded:
                raise
            ccf_parts.insert(
                0,
                (
                    "fallback",
                    self.ccf_fallback_provider.fetch_1m(
                        ccf_symbol,
                        ccf_fetch_start,
                        end_minute,
                    ),
                ),
            )
            index, ccf_report = self._prepare_ccf_seed(
                ccf_parts,
                end_minute=end_minute,
                ccf_fetch_start=ccf_fetch_start,
            )

        ccf = pd.Series(
            ccf_report.frame["merged_ccf_close"].to_numpy(),
            index=pd.DatetimeIndex(ccf_report.frame["timestamp"]),
        )
        ccf_filled = pd.Series(
            ccf_report.frame["ccf_close_filled"].to_numpy(),
            index=pd.DatetimeIndex(ccf_report.frame["timestamp"]),
        )
        start_minute = index[0].to_pydatetime()
        last_timestamp = index[-1].to_pydatetime()
        umc = close_series(
            self.umc_provider.fetch_ohlcv_1m(
                self.live_config.umc_symbol,
                start_minute,
                last_timestamp,
            ),
            "umc",
        ).reindex(index)
        usd = close_series(
            self.usd_twd_provider.fetch_ohlcv_1m(
                self.live_config.fx_symbol,
                start_minute,
                last_timestamp,
            ),
            "usd_twd",
        ).reindex(index)
        missing = umc[umc.isna()].index.union(usd[usd.isna()].index)
        if len(missing):
            raise RuntimeError(
                f"UMC/USDT-TWD warmup has missing minutes from {missing[0]}"
            )

        umc_twd_fair = umc * usd / 5.0
        spread = (
            (umc_twd_fair - ccf_filled)
            / (umc_twd_fair + ccf_filled)
            * 200.0
        )
        bars: list[MarketBar] = []
        for row_index, timestamp in enumerate(index):
            ccf_close = parse_optional_float(ccf.loc[timestamp])
            bars.append(
                MarketBar(
                    row_index=row_index - len(index),
                    timestamp=timestamp.to_pydatetime(),
                    ccf_close=ccf_close,
                    ccf_close_filled=float(ccf_filled.loc[timestamp]),
                    umc_twd_fair=float(umc_twd_fair.loc[timestamp]),
                    usd_twd=float(usd.loc[timestamp]),
                    spread=float(spread.loc[timestamp]),
                    ccf_was_filled=ccf_close is None,
                    ccf_symbol=ccf_symbol,
                    ccf_expiry=ccf_expiry,
                    contract_policy_state=contract_policy_state,
                )
            )
        return bars

    def _prepare_ccf_seed(
        self,
        ccf_parts: list[tuple[str, pd.DataFrame]],
        *,
        end_minute: datetime,
        ccf_fetch_start: datetime,
    ) -> tuple[pd.DatetimeIndex, CcfWarmupSourceReport]:
        index, session_index = build_ccf_expected_warmup_index(
            start=ccf_fetch_start,
            end=end_minute,
            count=self.live_config.warmup_minutes,
            closed_dates=self.closed_dates,
        )
        start_minute = index[0].to_pydatetime()
        ccf_report = build_ccf_warmup_source_report(
            ccf_parts,
            start_minute=start_minute,
            end_minute=end_minute,
            ccf_fetch_start=ccf_fetch_start,
            warmup_index=index,
            fill_index=session_index,
        )
        validate_ccf_warmup_report(
            ccf_report,
            max_trailing_fill_minutes=(
                self.live_config.warmup_ccf_max_trailing_fill_minutes
            ),
            max_forward_fill_ratio=self.live_config.warmup_forward_fill_max_ratio,
        )
        return index, ccf_report


def validate_ccf_warmup_report(
    ccf_report: CcfWarmupSourceReport,
    *,
    max_trailing_fill_minutes: int,
    max_forward_fill_ratio: float,
) -> None:
    """Fail closed when an expected CCF warmup window is missing or stale."""
    if ccf_report.null_count:
        first_missing = ccf_report.frame.loc[
            ccf_report.frame["ccf_close_filled"].isna(),
            "timestamp",
        ].iloc[0]
        raise RuntimeError(f"CCF warmup cannot forward-fill from {first_missing}")

    actual_ccf = ccf_report.frame["merged_ccf_close"].notna()
    if not actual_ccf.any():
        raise RuntimeError(
            "CCF warmup latest actual bar is stale: "
            "no actual CCF bar exists in the expected warmup window"
        )
    last_actual_position = int(actual_ccf[actual_ccf].index[-1])
    trailing_filled = len(ccf_report.frame) - last_actual_position - 1
    if trailing_filled > max_trailing_fill_minutes:
        last_actual_timestamp = ccf_report.frame.loc[
            last_actual_position,
            "timestamp",
        ]
        expected_end = ccf_report.frame.iloc[-1]["timestamp"]
        raise RuntimeError(
            "CCF warmup latest actual bar is stale: "
            f"last={last_actual_timestamp}, expected_end={expected_end}, "
            f"trailing_fill={trailing_filled} minutes exceeds max "
            f"{max_trailing_fill_minutes}"
        )

    # Data-quality gate: too many forward-filled CCF minutes means the feed
    # was mostly dead during warmup, so the rolling z-score is unreliable.
    total_minutes = len(ccf_report.frame)
    forward_filled = int(ccf_report.source_used_counts.get("forward_fill", 0))
    if total_minutes and max_forward_fill_ratio < 1.0:
        forward_fill_ratio = forward_filled / total_minutes
        if forward_fill_ratio > max_forward_fill_ratio:
            raise RuntimeError(
                "CCF warmup forward-fill ratio "
                f"{forward_fill_ratio:.3f} exceeds max "
                f"{max_forward_fill_ratio:.3f} "
                f"({forward_filled}/{total_minutes} minutes); refusing to "
                "seed the indicator on degraded warmup data"
            )


def build_ccf_warmup_source_report(
    frames: list[tuple[str, pd.DataFrame]],
    *,
    start_minute: datetime,
    end_minute: datetime,
    ccf_fetch_start: datetime,
    warmup_index: pd.DatetimeIndex | None = None,
    fill_index: pd.DatetimeIndex | None = None,
) -> CcfWarmupSourceReport:
    start_minute = floor_minute(start_minute)
    end_minute = floor_minute(end_minute)
    ccf_fetch_start = floor_minute(ccf_fetch_start)
    if warmup_index is None:
        warmup_index = pd.date_range(start_minute, end_minute, freq="min")
    else:
        warmup_index = pd.DatetimeIndex(warmup_index)
    if fill_index is None:
        fill_index = pd.date_range(ccf_fetch_start, end_minute, freq="min")
    else:
        fill_index = pd.DatetimeIndex(fill_index)

    source_series = {
        source: close_series(frame, source)
        for source, frame in frames
    }
    combined = prioritized_ccf_close_frame(frames)

    report = pd.DataFrame({"timestamp": warmup_index})
    for source, series in source_series.items():
        report[f"{source}_close"] = series.reindex(warmup_index).to_numpy()
    report["merged_ccf_close"] = combined["close"].reindex(warmup_index).to_numpy()
    filled = combined["close"].reindex(fill_index).ffill().reindex(warmup_index)
    direct_source = combined["source"].reindex(warmup_index)
    report["ccf_close_filled"] = filled.to_numpy()
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

    return CcfWarmupSourceReport(
        frame=report,
        start=start_minute,
        end=end_minute,
        ccf_fetch_start=ccf_fetch_start,
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
        null_count=int(report["ccf_close_filled"].isna().sum()),
        overlap_rows=overlap_rows,
        mismatch_count=mismatch_count,
        max_abs_diff=max_abs_diff,
    )

