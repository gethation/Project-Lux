"""Probe the Twelve Data free tier for USD/TWD.

Resolves open item #5 in docs/MULTIPAIR_PLAN.md: the pair was known to exist
(symbol_search is public) but the free tier's actual interval support, freshness,
and behaviour during the UMC session were never measured with a real key.

Reads TWELVEDATA_API_KEY from the repo's .env and never prints it.

    conda run -n Quant python scripts/probe_twelvedata.py

Re-run inside the UMC session (Taipei 21:30-04:00 in summer) to close the last
gap: the first measurement landed outside it.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


TAIPEI = timezone(timedelta(hours=8))
BASE = "https://api.twelvedata.com"
SYMBOL = "USD/TWD"


def load_key() -> str:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        raise SystemExit(f"{env_path} does not exist")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "TWELVEDATA_API_KEY":
            return value.strip()
    raise SystemExit("TWELVEDATA_API_KEY not found in .env")


def get(path: str, key: str, **params) -> tuple[dict, float]:
    params["apikey"] = key
    started = datetime.now()
    response = requests.get(f"{BASE}/{path}", params=params, timeout=20)
    elapsed = (datetime.now() - started).total_seconds()
    try:
        return response.json(), elapsed
    except ValueError:
        return {"_raw": response.text[:400], "_status": response.status_code}, elapsed


def parse_ts(text: str) -> datetime:
    """Twelve Data returns naive timestamps in the requested timezone."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"unparsable timestamp: {text!r}")


def show_error(label: str, payload: dict) -> bool:
    if payload.get("status") == "error" or "code" in payload:
        print(f"  {label}: ERROR code={payload.get('code')} {payload.get('message')}")
        return True
    if "_raw" in payload:
        print(f"  {label}: non-JSON status={payload.get('_status')} {payload['_raw'][:200]}")
        return True
    return False


def main() -> int:
    key = load_key()
    now_tpe = datetime.now(TAIPEI)
    print(f"Taipei now : {now_tpe:%Y-%m-%d %H:%M:%S} (UTC+8)")
    print(f"Symbol     : {SYMBOL}\n")

    print("=== 1. /quote (what a live poll would call) ===")
    quote, elapsed = get("quote", key, symbol=SYMBOL, timezone="Asia/Taipei")
    if not show_error("quote", quote):
        ts = quote.get("datetime") or quote.get("timestamp")
        print(f"  rate        {quote.get('close')}   (open {quote.get('open')}, "
              f"high {quote.get('high')}, low {quote.get('low')})")
        print(f"  datetime    {ts}")
        print(f"  is_market_open {quote.get('is_market_open')}")
        print(f"  http latency  {elapsed:.2f}s")
        if ts:
            try:
                age = now_tpe.replace(tzinfo=None) - parse_ts(str(ts))
                print(f"  DATA AGE      {age.total_seconds() / 60:.1f} minutes")
            except ValueError as exc:
                print(f"  (age unknown: {exc})")

    for interval in ("1min", "5min", "15min"):
        print(f"\n=== 2. /time_series interval={interval} ===")
        series, elapsed = get(
            "time_series",
            key,
            symbol=SYMBOL,
            interval=interval,
            outputsize=5,
            timezone="Asia/Taipei",
        )
        if show_error(interval, series):
            continue
        values = series.get("values") or []
        print(f"  bars returned {len(values)}   http {elapsed:.2f}s")
        for row in values[:5]:
            print(f"    {row['datetime']}  close={row['close']}")
        if values:
            newest = parse_ts(values[0]["datetime"])
            age = now_tpe.replace(tzinfo=None) - newest
            print(f"  NEWEST BAR AGE {age.total_seconds() / 60:.1f} minutes")
            if len(values) > 1:
                gap = newest - parse_ts(values[1]["datetime"])
                print(f"  spacing        {gap.total_seconds() / 60:.0f} min "
                      f"(expected {interval})")

    print("\n=== 3. free-tier quota ===")
    usage, _ = get("api_usage", key)
    if not show_error("api_usage", usage):
        print(f"  {usage}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
