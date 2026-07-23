from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lux_trader.core.time import TAIPEI_TZ  # noqa: E402
from lux_trader.integrations.ibkr.client_process import IbkrClientProcess  # noqa: E402
from lux_trader.integrations.ibkr.historical import (  # noqa: E402
    BAR_COLUMNS,
    RTH_BARS_PER_SESSION,
    fetch_umc_1m_history,
    session_days,
)


DEFAULT_OUT = Path(
    r"D:\Users\Documents\Proof of Concept\data\processed\umc_1m_cumulative.csv"
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Accumulate NYSE:UMC 1-minute RTH bars into a cumulative CSV. "
            "Unlike the TAIFEX feed there is no rolling-window deadline here "
            "(IBKR serves at least two years), so this favours correctness over "
            "coverage: it merges rather than overwrites, reports conflicting "
            "minutes instead of silently replacing them, and flags any session "
            "that is not a complete 390-bar RTH day."
        )
    )
    parser.add_argument("--months", type=int, default=3)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--client-id", type=int, default=17_004)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and report what would change without writing the file.",
    )
    return parser.parse_args(argv)


def read_existing(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=BAR_COLUMNS)
    frame = pd.read_csv(path)
    missing = set(BAR_COLUMNS).difference(frame.columns)
    if missing:
        raise RuntimeError(f"{path} is missing columns: {sorted(missing)}")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
        TAIPEI_TZ
    )
    return frame[BAR_COLUMNS].sort_values("timestamp").reset_index(drop=True)


def format_timestamps(values: pd.Series) -> pd.Series:
    formatted = values.dt.strftime("%Y-%m-%d %H:%M:%S%z")
    return formatted.str.replace(r"(\+|-)(\d{2})(\d{2})$", r"\1\2:\3", regex=True)


def report_conflicts(existing: pd.DataFrame, fresh: pd.DataFrame) -> int:
    """Minutes present in both with different values.

    A settled RTH minute should re-fetch identically, so a conflict means the
    earlier capture was partial or the feed revised itself. Fresh data wins, but
    never silently.
    """
    if existing.empty or fresh.empty:
        return 0
    overlap = existing.merge(fresh, on="timestamp", suffixes=("_old", "_new"))
    if overlap.empty:
        return 0
    differs = pd.Series(False, index=overlap.index)
    for column in ("open", "high", "low", "close", "volume"):
        differs |= (
            (overlap[f"{column}_old"] - overlap[f"{column}_new"]).abs() > 1e-9
        )
    conflicts = overlap[differs]
    if conflicts.empty:
        return 0
    print(
        f"WARNING: {len(conflicts):,} overlapping minutes differ from the "
        "cumulative file; fresh values win. Sample:"
    )
    for _, row in conflicts.head(5).iterrows():
        print(
            f"  {row['timestamp']}  close {row['close_old']} -> {row['close_new']}"
            f"  volume {row['volume_old']} -> {row['volume_new']}"
        )
    return len(conflicts)


def summarize(frame: pd.DataFrame, label: str) -> None:
    if frame.empty:
        print(f"{label}: empty")
        return
    days = session_days(frame)
    print(
        f"{label}: {len(frame):,} bars, {days.nunique()} sessions, "
        f"{frame['timestamp'].min()} -> {frame['timestamp'].max()}"
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    out_path: Path = args.out

    existing = read_existing(out_path)
    summarize(existing, "Existing")

    print(f"\nFetching {args.months} month(s) of UMC 1m RTH bars from IBKR...")
    client = IbkrClientProcess(
        host=args.host, port=args.port, client_id=args.client_id
    )
    try:
        health = client.connect()
        if not health.get("connected"):
            print(f"ERROR: {health.get('message')}", file=sys.stderr)
            return 2
        print(f"  connected: server {health.get('server_version')}")
        fresh, report = fetch_umc_1m_history(
            client, months=args.months, end=datetime.now(UTC)
        )
    finally:
        client.close()

    print()
    for line in report.summary_lines():
        print(f"  {line}")
    if fresh.empty:
        print("\nNothing fetched; cumulative file left unchanged.")
        return 1

    conflicts = report_conflicts(existing, fresh)

    combined = pd.concat([existing, fresh], ignore_index=True)
    # concat with an empty object-dtype frame (first run) degrades the column
    combined["timestamp"] = pd.to_datetime(
        combined["timestamp"], utc=True
    ).dt.tz_convert(TAIPEI_TZ)
    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    combined = combined.sort_values("timestamp").reset_index(drop=True)

    timestamps = pd.DatetimeIndex(combined["timestamp"])
    if timestamps.has_duplicates:
        raise RuntimeError("Merged output has duplicate timestamps")
    if not timestamps.is_monotonic_increasing:
        raise RuntimeError("Merged output is not sorted")

    added = len(combined) - len(existing)
    new_days = set(session_days(combined)) - set(session_days(existing))
    print()
    summarize(combined, "Merged")
    print(
        f"Added {added:,} bars and {len(new_days)} new sessions"
        + (f"; {conflicts:,} minutes corrected" if conflicts else "")
    )
    if report.incomplete_sessions:
        print(
            f"NOTE: {len(report.incomplete_sessions)} session(s) are not "
            f"{RTH_BARS_PER_SESSION} bars. Investigate before using them; do not "
            "forward-fill."
        )

    if args.dry_run:
        print(f"\n--dry-run: {out_path} not written")
        return 0

    output = combined.copy()
    output["timestamp"] = format_timestamps(output["timestamp"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(out_path, index=False)
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
