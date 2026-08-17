"""Read-only HTTP server for the spread chart.

Standard library only -- no new dependency, nothing to install into the Quant
env, and nothing that could be mistaken for part of the trading path. It binds
to 127.0.0.1 by default because this serves a live position's P&L.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import AppConfig
from ..core.sizing import umc_contract_twd_price
from ..core.tradable_spread import estimate_zscore, spread_from_prices
from .queries import (
    INTERVAL_SECONDS,
    UMC_TWD_FAIR_DIVISOR,
    Candle,
    StoreReader,
    aggregate_candles,
    as_float,
    bucket_last,
    bucket_start,
    display_epoch,
    minute_candles,
    parse_ts,
    threshold_series,
    tick_spreads,
    tz_offset_seconds,
)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_LIMIT = 1500
MAX_LIMIT = 20000

BAR_FIELDS = (
    "spread",
    "spread_mean",
    "spread_std",
    "spread_zscore",
    "short_spread",
    "short_zscore",
    "long_spread",
    "long_zscore",
    "unrealized_pnl",
    "equity",
    "realized_pnl",
)


class ChartService:
    """Everything the page can ask for, computed from a read-only store."""

    def __init__(self, config: AppConfig, reader: StoreReader) -> None:
        self.config = config
        self.reader = reader
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        summary = self.reader.store_summary()
        return {
            # Without ticks a 1m candle can only be open==high==low==close --
            # a row of flat marks that looks like data and is not. Start such a
            # store on 5m, where aggregating one-minute closes gives a real range.
            "default_interval": "1m" if summary["market_ticks"] else "5m",
            "store_path": str(self.reader.path),
            "store_exists": self.reader.path.exists(),
            "ccf_symbol": self.config.live.ccf_symbol,
            "umc_symbol": self.config.live.umc_symbol,
            "fx_symbol": self.config.live.fx_symbol,
            "entry_z": self.config.strategy.entry_z,
            "exit_z": self.config.strategy.exit_z,
            "zscore_window": self.config.strategy.zscore_window,
            "ccf_lots": self.config.strategy.ccf_lots,
            "max_entry_delay_minutes": self.config.strategy.max_entry_delay_minutes,
            "intervals": list(INTERVAL_SECONDS),
            "summary": summary,
        }

    # ------------------------------------------------------------------
    def chart(self, interval: str, limit: int) -> dict[str, Any]:
        seconds = INTERVAL_SECONDS.get(interval)
        if seconds is None:
            raise ValueError(f"unsupported interval: {interval}")
        # Aggregated views need proportionally more minutes behind them, or a
        # "last 1500" 15m request silently becomes a hundred candles.
        bar_limit = min(MAX_LIMIT, max(limit, limit * seconds // 60))
        bars = self.reader.latest_bars(bar_limit)
        if not bars:
            return {
                "interval": interval,
                "candles": [],
                "band": [],
                "z": [],
                "thresholds": [],
                "markers": [],
                "source": "empty",
                "tz_offset_seconds": 0,
            }

        first_timestamp = str(bars[0]["timestamp"])
        ticks = tick_spreads(
            self.reader.ticks_since(first_timestamp, self.config.live.fx_symbol)
        )
        candles, reconstructed = minute_candles(bars, ticks)
        candles = aggregate_candles(candles, seconds)

        stats = bucket_last(bars, seconds, BAR_FIELDS)
        band = [
            {
                "time": row["time"],
                "short": row["short_spread"],
                "long": row["long_spread"],
            }
            for row in stats
        ]
        z_rows = [
            {
                "time": row["time"],
                "short_z": row["short_zscore"],
                "long_z": row["long_zscore"],
                "mid_z": row["spread_zscore"],
            }
            for row in stats
        ]
        return {
            "interval": interval,
            "interval_seconds": seconds,
            "source": "ticks+bars" if reconstructed else "bars",
            "intrabar": reconstructed,
            "tz_offset_seconds": tz_offset_seconds(bars),
            "candles": [candle.to_json() for candle in candles],
            "band": band,
            "z": z_rows,
            "thresholds": threshold_series(
                stats,
                self.config.strategy.entry_z,
                self.config.strategy.exit_z,
            ),
            "markers": self.markers(seconds, candles),
            "equity": [
                {"time": row["time"], "value": row["equity"]}
                for row in stats
                if row["equity"] is not None
            ],
        }

    # ------------------------------------------------------------------
    def markers(self, seconds: int, candles: list[Candle]) -> list[dict[str, Any]]:
        """Four markers per trade: signal and fill, on both ends.

        Collapsing them to two would hide the delay between deciding and being
        filled -- which is where the execution haircut lives, and which the
        strategy deliberately spends a whole bar on (strategy.py:44-49).
        """
        available = {candle.time for candle in candles}

        def snap(value: str | None) -> int | None:
            if not value:
                return None
            slot = bucket_start(display_epoch(parse_ts(str(value))), seconds)
            return slot if slot in available else None

        markers: list[dict[str, Any]] = []
        for trade in self.reader.trades():
            direction = str(trade["direction"])
            is_short = direction == "short_umc_long_ccf"
            pnl = as_float(trade["net_pnl_twd"]) or 0.0
            reason = str(trade["exit_reason"])
            forced = reason != "zscore_exit"
            delay = trade["entry_delay_minutes"]

            entry_signal = snap(trade["entry_signal_time"])
            if entry_signal is not None:
                markers.append(
                    {
                        "time": entry_signal,
                        "kind": "entry_signal",
                        "direction": direction,
                        "position": "aboveBar" if is_short else "belowBar",
                        "shape": "circle",
                        # No label: the fill marker carries both z values, so
                        # labelling the signal too doubles the text on a chart
                        # that already draws four markers per trade.
                        "text": "",
                    }
                )
            entry_fill = snap(trade["entry_time"])
            if entry_fill is not None:
                markers.append(
                    {
                        "time": entry_fill,
                        "kind": "entry_fill",
                        "direction": direction,
                        "position": "aboveBar" if is_short else "belowBar",
                        "shape": "arrowDown" if is_short else "arrowUp",
                        # signal z -> fill z is the execution haircut, which is
                        # the whole reason the two are recorded separately.
                        "text": (
                            f"{'SHORT' if is_short else 'LONG'} "
                            f"{fmt(trade['entry_signal_zscore'])}"
                            f"→{fmt(trade['entry_fill_zscore'])}"
                            + (f" +{delay}m" if delay else "")
                        ),
                    }
                )
            exit_signal = snap(trade["exit_signal_time"])
            if exit_signal is not None:
                markers.append(
                    {
                        "time": exit_signal,
                        "kind": "exit_signal",
                        "direction": direction,
                        "position": "belowBar" if is_short else "aboveBar",
                        "shape": "circle",
                        "text": "",
                    }
                )
            exit_fill = snap(trade["exit_time"])
            if exit_fill is not None:
                markers.append(
                    {
                        "time": exit_fill,
                        "kind": "exit_fill",
                        "direction": direction,
                        "profit": pnl >= 0,
                        "forced": forced,
                        "position": "belowBar" if is_short else "aboveBar",
                        "shape": "square" if forced else ("arrowUp" if is_short else "arrowDown"),
                        "text": f"exit {pnl:+,.0f}" + (f" · {reason}" if forced else ""),
                    }
                )
        markers.sort(key=lambda item: item["time"])
        return markers

    # ------------------------------------------------------------------
    def live(self) -> dict[str, Any]:
        now = datetime.now().astimezone()
        state = self.reader.strategy_state() or {}
        bars = self.reader.latest_bars(1)
        bar = dict(bars[-1]) if bars else None
        tick = self.reader.latest_tick(self.config.live.fx_symbol)

        payload: dict[str, Any] = {
            "server_time": now.isoformat(),
            "state": state.get("state"),
            "position_direction": state.get("position_direction"),
            "candidate_direction": state.get("candidate_direction"),
            "candidate_time": state.get("candidate_time"),
            "candidate_zscore": state.get("candidate_zscore"),
            "exit_signal_time": state.get("exit_signal_time"),
            "exit_signal_zscore": state.get("exit_signal_zscore"),
            "entry_zscore": state.get("entry_zscore"),
            "umc_units": state.get("umc_units"),
            "ccf_contracts": state.get("ccf_contracts"),
            "realized_pnl": state.get("realized_pnl"),
            "trading_ccf_symbol": state.get("trading_ccf_symbol"),
            "bar": None,
            "tick": None,
            "entry_spread": None,
            "unrealized_pnl": None,
            "unrealized_source": None,
        }

        if bar is not None:
            payload["bar"] = {
                "time": str(bar["timestamp"]),
                "epoch": display_epoch(parse_ts(str(bar["timestamp"]))),
                "spread": as_float(bar["spread"]),
                "mean": as_float(bar["spread_mean"]),
                "std": as_float(bar["spread_std"]),
                "mid_z": as_float(bar["spread_zscore"]),
                "short_spread": as_float(bar["short_spread"]),
                "short_z": as_float(bar["short_zscore"]),
                "long_spread": as_float(bar["long_spread"]),
                "long_z": as_float(bar["long_zscore"]),
                "unrealized_pnl": as_float(bar["unrealized_pnl"]),
                "equity": as_float(bar["equity"]),
                "state": str(bar["state"]),
                "age_seconds": max(
                    0.0, (now - parse_ts(str(bar["timestamp"]))).total_seconds()
                ),
            }
            payload["unrealized_pnl"] = as_float(bar["unrealized_pnl"])
            payload["unrealized_source"] = "bar"

        entry_umc = as_float(state.get("entry_umc"))
        entry_ccf = as_float(state.get("entry_ccf"))
        if entry_umc is not None and entry_ccf is not None:
            payload["entry_spread"] = spread_from_prices(
                umc_contract_twd_price(entry_umc, self.config.fees)
                / UMC_TWD_FAIR_DIVISOR,
                entry_ccf,
            )

        if tick is not None:
            payload["tick"] = self._tick_payload(tick, now, bar)
            live_upnl = self._live_unrealized(state, payload["tick"])
            if live_upnl is not None:
                payload["unrealized_pnl"] = live_upnl
                payload["unrealized_source"] = "tick"

        short_z = (payload.get("tick") or {}).get("short_z")
        long_z = (payload.get("tick") or {}).get("long_z")
        if short_z is None or long_z is None:
            short_z = (payload.get("bar") or {}).get("short_z")
            long_z = (payload.get("bar") or {}).get("long_z")
        payload["band_width_z"] = (
            long_z - short_z if short_z is not None and long_z is not None else None
        )
        return payload

    # ------------------------------------------------------------------
    def _tick_payload(
        self,
        tick: dict[str, Any],
        now: datetime,
        bar: dict[str, Any] | None,
    ) -> dict[str, Any]:
        ccf = as_float(tick.get("ccf"))
        umc = as_float(tick.get("umc"))
        fx = as_float(tick.get("fx"))
        mean = as_float(bar["spread_mean"]) if bar else None
        std = as_float(bar["spread_std"]) if bar else None

        def z_of(spread: float | None) -> float | None:
            if spread is None or mean is None or std in (None, 0.0):
                return None
            return (spread - mean) / std

        mid = None
        short = None
        long_ = None
        if ccf and umc and fx:
            mid = spread_from_prices(umc * fx / UMC_TWD_FAIR_DIVISOR, ccf)
            umc_bid = as_float(tick.get("umc_bid"))
            umc_ask = as_float(tick.get("umc_ask"))
            ccf_bid = as_float(tick.get("ccf_bid"))
            ccf_ask = as_float(tick.get("ccf_ask"))
            if umc_bid and ccf_ask:
                short = spread_from_prices(umc_bid * fx / UMC_TWD_FAIR_DIVISOR, ccf_ask)
            if umc_ask and ccf_bid:
                long_ = spread_from_prices(umc_ask * fx / UMC_TWD_FAIR_DIVISOR, ccf_bid)

        observed_at = str(tick.get("observed_at"))
        return {
            "observed_at": observed_at,
            # The engine commits once per finalized minute (engine.py:542), so a
            # read-only reader never sees the current second. Report the age
            # instead of implying this is now.
            "age_seconds": max(0.0, (now - parse_ts(observed_at)).total_seconds()),
            "ccf": ccf,
            "ccf_bid": as_float(tick.get("ccf_bid")),
            "ccf_ask": as_float(tick.get("ccf_ask")),
            "umc": umc,
            "umc_bid": as_float(tick.get("umc_bid")),
            "umc_ask": as_float(tick.get("umc_ask")),
            "fx": fx,
            "mid_spread": mid,
            "short_spread": short,
            "long_spread": long_,
            "mid_z": z_of(mid),
            "short_z": z_of(short),
            "long_z": z_of(long_),
        }

    def _live_unrealized(
        self,
        state: dict[str, Any],
        tick: dict[str, Any],
    ) -> float | None:
        """Mark the open position at the newest committed tick.

        Same formula as strategy.py:774-779, on purpose: the panel must not
        invent a second definition of unrealized P&L. It is still the MODEL's
        number, not the broker's -- the broker's lives only in memory
        (margin/display.py) and never reaches this store.
        """
        entry_umc = as_float(state.get("entry_umc"))
        entry_ccf = as_float(state.get("entry_ccf"))
        umc_units = as_float(state.get("umc_units")) or 0.0
        ccf_units = as_float(state.get("ccf_units")) or 0.0
        if entry_umc is None or entry_ccf is None or not state.get("position_direction"):
            return None
        umc = tick.get("umc")
        fx = tick.get("fx")
        ccf = tick.get("ccf")
        if umc is None or fx is None or ccf is None:
            return None
        umc_twd_fair = umc * fx / UMC_TWD_FAIR_DIVISOR
        return umc_units * (
            umc_contract_twd_price(umc_twd_fair, self.config.fees)
            - umc_contract_twd_price(entry_umc, self.config.fees)
        ) + ccf_units * (ccf - entry_ccf)


def fmt(value: Any) -> str:
    return "-" if value is None else f"{float(value):.2f}"


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    return host.strip() in LOOPBACK_HOSTS


class ChartRequestHandler(BaseHTTPRequestHandler):
    service: ChartService
    token: str | None = None
    server_version = "LuxWeb/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)
        try:
            if not self._authorized(params):
                self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized")
                return
            if route in ("/", "/index.html"):
                self._send_static("index.html")
            elif route.startswith("/static/"):
                self._send_static(route[len("/static/") :])
            elif route == "/api/meta":
                self._send_json(self.service.meta())
            elif route == "/api/live":
                self._send_json(self.service.live())
            elif route == "/api/chart":
                interval = params.get("interval", ["1m"])[0]
                limit = min(MAX_LIMIT, max(1, int(params.get("limit", [DEFAULT_LIMIT])[0])))
                self._send_json(self.service.chart(interval, limit))
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except ConnectionError:
            # The browser cancels in-flight requests all the time (reload, tab
            # switch). Logging a traceback for each one buries real errors.
            self.close_connection = True
        except FileNotFoundError:
            self._send_error(HTTPStatus.NOT_FOUND, "not found")
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:  # a broken panel must not take the server down
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}"
            )

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    # ------------------------------------------------------------------
    def _authorized(self, params: dict[str, list[str]]) -> bool:
        """Shared-secret check, in the Jupyter style: `?token=` once, cookie after.

        Only armed when a token is configured, which serve() does automatically
        for any non-loopback bind. Constant-time compare so the check itself
        does not leak the secret.
        """
        if not self.token:
            return True
        supplied = None
        values = params.get("token")
        if values:
            supplied = values[0]
        if supplied is None:
            supplied = self.headers.get("X-Lux-Token")
        if supplied is None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "lux_token":
                    supplied = value
                    break
        return supplied is not None and hmac.compare_digest(supplied, self.token)

    def _send_common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        if self.token:
            self.send_header(
                "Set-Cookie",
                f"lux_token={self.token}; Path=/; SameSite=Strict; Max-Age=604800",
            )

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _send_static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            raise FileNotFoundError(name)
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith(("text/", "application/javascript")):
            content_type = f"{content_type}; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def serve(
    config: AppConfig,
    *,
    host: str,
    port: int,
    token: str | None = None,
) -> None:
    reader = StoreReader(config.store_path)
    # A non-loopback bind puts a live position's size and P&L on the network, so
    # it never goes out unauthenticated: mint a token if the operator did not
    # supply one. Loopback stays token-free -- the OS is the boundary there.
    if token is None and not is_loopback(host):
        token = secrets.token_urlsafe(24)
    handler = type(
        "BoundChartRequestHandler",
        (ChartRequestHandler,),
        {"service": ChartService(config, reader), "token": token},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    suffix = f"/?token={token}" if token else ""
    print(f"lux web (read-only) → http://{display_host}:{port}{suffix}")
    if token:
        print(f"token: {token}")
        print("NOTE: plain HTTP, no TLS. Put it behind a VPN, not the open internet.")
    print(f"store: {config.store_path}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
