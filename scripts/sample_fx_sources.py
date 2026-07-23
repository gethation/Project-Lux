"""Sample both FX sources repeatedly so the freshness comparison is not one draw.

Records, for each poll, the newest 1m bar each source offers and how old it is.
The number that matters is not the raw age but whether the bar covering minute M
is available shortly after M ends -- that is what the minute-bar staleness gate
actually asks.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TAIPEI = timezone(timedelta(hours=8))
SAMPLES = 20
INTERVAL_SECONDS = 30
OUT = Path(__file__).with_name("fx_sampling.csv")


def load_key() -> str:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "TWELVEDATA_API_KEY":
            return value.strip()
    raise SystemExit("key missing")


KEY = load_key()


def twelvedata_newest() -> tuple[datetime | None, float | None, str]:
    try:
        payload = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": "USD/TWD",
                "interval": "1min",
                "outputsize": 1,
                "timezone": "Asia/Taipei",
                "apikey": KEY,
            },
            timeout=15,
        ).json()
    except Exception as exc:
        return None, None, f"{type(exc).__name__}"
    values = payload.get("values") or []
    if not values:
        return None, None, str(payload.get("message", payload))[:60]
    row = values[0]
    return (
        datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S"),
        float(row["close"]),
        "ok",
    )


def tvdatafeed_newest(tv) -> tuple[datetime | None, float | None, str]:
    from tvDatafeed import Interval

    try:
        frame = tv.get_hist(
            symbol="USDTWD", exchange="FX_IDC", interval=Interval.in_1_minute, n_bars=1
        )
    except Exception as exc:
        return None, None, f"{type(exc).__name__}"
    if frame is None or frame.empty:
        return None, None, "empty"
    return frame.index[-1].to_pydatetime(), float(frame.iloc[-1]["close"]), "ok"


def main() -> int:
    from tvDatafeed import TvDatafeed

    tv = TvDatafeed()
    rows = ["poll_at,td_bar,td_age_s,td_close,tv_bar,tv_age_s,tv_close,td_status,tv_status"]
    print(f"{'poll':<10}{'TD bar':<10}{'age':>6}  {'TV bar':<10}{'age':>6}  "
          f"{'TD close':>10}{'TV close':>10}{'diff%':>8}")

    for index in range(SAMPLES):
        now = datetime.now(TAIPEI).replace(tzinfo=None)
        td_bar, td_close, td_status = twelvedata_newest()
        tv_bar, tv_close, tv_status = tvdatafeed_newest(tv)

        td_age = (now - td_bar).total_seconds() if td_bar else float("nan")
        tv_age = (now - tv_bar).total_seconds() if tv_bar else float("nan")
        diff = (
            abs(td_close - tv_close) / tv_close * 100
            if td_close and tv_close
            else float("nan")
        )
        print(
            f"{now:%H:%M:%S}  {td_bar:%H:%M} {td_age:>6.0f}  "
            f"{tv_bar:%H:%M} {tv_age:>6.0f}  "
            f"{td_close:>10.5f}{tv_close:>10.5f}{diff:>8.3f}"
            if td_bar and tv_bar
            else f"{now:%H:%M:%S}  TD={td_status} TV={tv_status}"
        )
        rows.append(
            f"{now:%Y-%m-%d %H:%M:%S},{td_bar},{td_age},{td_close},"
            f"{tv_bar},{tv_age},{tv_close},{td_status},{tv_status}"
        )
        if index < SAMPLES - 1:
            time.sleep(INTERVAL_SECONDS)

    OUT.write_text("\n".join(rows), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
