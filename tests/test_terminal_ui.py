from __future__ import annotations

import io
from datetime import datetime

import pytest

from lux_trader.cli import build_parser
from lux_trader.core.indicator import IndicatorEngine
from lux_trader.market_data import LiveQuote, LiveQuoteSet
from lux_trader.core.models import MarketBar, StrategyAction, StrategyState
from lux_trader.terminal_ui import (
    LiveTerminalReporter,
    format_countdown,
)
from lux_trader.core.tradable_spread import TradableSpreadSnapshot, estimate_tradable_spreads


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def quote(
    source: str,
    timestamp: str,
    price: float,
    *,
    bid: float | None = None,
    ask: float | None = None,
) -> LiveQuote:
    return LiveQuote(
        source=source,
        symbol=source,
        timestamp=ts(timestamp),
        price=price,
        bid=bid,
        ask=ask,
    )


def bar(spread: float) -> MarketBar:
    return MarketBar(
        row_index=0,
        timestamp=ts("2026-06-18T09:00:00+08:00"),
        qff_close=100.0,
        qff_close_filled=100.0,
        tsm_twd_fair=100.0,
        spread=spread,
    )


def test_live_terminal_reporter_refreshes_live_line_without_newlines() -> None:
    stream = io.StringIO()
    reporter = LiveTerminalReporter(stream, color=False)

    reporter.live(
        ts("2026-06-18T09:12:04+08:00"),
        TradableSpreadSnapshot(
            mid_spread=1.842731,
            mid_zscore=1.7342,
            short_spread=1.62,
            short_zscore=1.51,
            long_spread=2.06,
            long_zscore=1.93,
        ),
        StrategyState.FLAT,
    )
    reporter.live(
        ts("2026-06-18T09:12:05+08:00"),
        TradableSpreadSnapshot(
            mid_spread=1.856104,
            mid_zscore=None,
            short_spread=None,
            short_zscore=None,
            long_spread=2.06,
            long_zscore=1.93,
        ),
        StrategyState.OPEN,
    )

    output = stream.getvalue()
    assert output.count("\n") == 0
    assert output.count("\r") == 2
    assert (
        "09:12:05 LIVE mid=1.86 "
        "shortSpread(spread=NA,z=NA) "
        "longSpread(spread=2.06,z=1.93) OPEN"
    ) in output


def test_live_terminal_reporter_refreshes_non_trading_line() -> None:
    stream = io.StringIO()
    reporter = LiveTerminalReporter(stream, color=False)

    reporter.live_non_trading(
        ts("2026-06-20T02:31:04+08:00"),
        ts("2026-06-22T08:45:00+08:00"),
        "closed_date",
    )
    reporter.live_non_trading(
        ts("2026-06-20T02:31:05+08:00"),
        ts("2026-06-22T08:45:00+08:00"),
        "closed_date",
    )

    output = stream.getvalue()
    assert output.count("\n") == 0
    assert output.count("\r") == 2
    assert (
        "02:31:05 LIVE non-trading session next=06/22 08:45 in=54:13:55"
    ) in output


def test_live_terminal_reporter_non_trading_color_and_countdown_over_24h() -> None:
    stream = io.StringIO()
    reporter = LiveTerminalReporter(stream, color=True)

    reporter.live_non_trading(
        ts("2026-06-20T02:31:04+08:00"),
        ts("2026-06-22T08:45:00+08:00"),
        "closed_date",
    )

    output = stream.getvalue()
    assert "\x1b[33mLIVE non-trading session\x1b[0m" in output
    assert format_countdown(
        ts("2026-06-20T02:31:04+08:00"),
        ts("2026-06-22T08:45:00+08:00"),
    ) == "54:13:56"


def test_live_terminal_reporter_clears_live_before_permanent_lines() -> None:
    stream = io.StringIO()
    reporter = LiveTerminalReporter(stream, color=False)

    spread_snapshot = TradableSpreadSnapshot(
        mid_spread=1.861422,
        mid_zscore=1.7781,
        short_spread=1.62,
        short_zscore=1.51,
        long_spread=2.06,
        long_zscore=1.93,
    )
    reporter.live(ts("2026-06-18T09:12:04+08:00"), spread_snapshot, StrategyState.FLAT)
    reporter.bar(
        ts("2026-06-18T09:12:00+08:00"),
        spread_snapshot,
        StrategyState.FLAT,
        StrategyAction.NONE,
        "no_action",
        0.0,
        1_000_000.0,
    )
    reporter.warn(ts("2026-06-18T09:13:23+08:00"), "stale_tsm", "skipped_minute")
    reporter.event(ts("2026-06-18T09:14:00+08:00"), "entry_signal", "zscore_crossed")
    reporter.error(ts("2026-06-18T09:31:08+08:00"), "RuntimeError: Fubon quote fetch failed")

    output = stream.getvalue()
    assert (
        "09:12 BAR  mid=1.86 z=1.78 "
        "shortSpread(spread=1.62,z=1.51) "
        "longSpread(spread=2.06,z=1.93) FLAT none pnl=0 eq=1,000,000\n"
    ) in output
    assert "09:13:23 WARN stale_tsm skipped_minute\n" in output
    assert "09:14:00 EVENT entry_signal zscore_crossed\n" in output
    assert "09:31:08 ERR RuntimeError: Fubon quote fetch failed\n" in output
    assert "\x1b[" not in output


def test_live_terminal_reporter_compacts_action_reason_and_supports_color() -> None:
    stream = io.StringIO()
    reporter = LiveTerminalReporter(stream, color=True)

    reporter.bar(
        ts("2026-06-18T09:14:00+08:00"),
        TradableSpreadSnapshot(
            mid_spread=2.243801,
            mid_zscore=2.0614,
            short_spread=2.18,
            short_zscore=2.00,
            long_spread=2.31,
            long_zscore=2.17,
        ),
        StrategyState.ENTRY_PENDING,
        StrategyAction.ENTRY_SIGNAL,
        "entry_zscore_crossed",
        0.0,
        1_000_000.0,
    )

    output = stream.getvalue()
    assert "entry_signal/zscore_crossed" in output
    assert "\x1b[" in output


def test_tradable_spread_uses_bid_ask_and_does_not_mutate_indicator() -> None:
    indicator = IndicatorEngine(window=3)
    for spread in (1.0, 2.0, 3.0):
        indicator.update(bar(spread))
    before = indicator.to_jsonable()
    quote_set = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T09:00:10+08:00", 100.0, bid=99.0, ask=101.0),
        tsm=quote("tsm", "2026-06-18T09:00:10+08:00", 20.0, bid=19.5, ask=20.5),
        usdttwd=quote("usd", "2026-06-18T09:00:10+08:00", 30.0, bid=29.9, ask=30.1),
    )

    snapshot = estimate_tradable_spreads(
        quote_set,
        ts("2026-06-18T09:00:10+08:00"),
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )

    short_spread = ((19.5 * 29.9 / 5.0) - 101.0) / ((19.5 * 29.9 / 5.0) + 101.0) * 200.0
    long_spread = ((20.5 * 30.1 / 5.0) - 99.0) / ((20.5 * 30.1 / 5.0) + 99.0) * 200.0
    assert snapshot.short_spread == pytest.approx(short_spread)
    assert snapshot.long_spread == pytest.approx(long_spread)
    assert snapshot.short_zscore is not None
    assert snapshot.long_zscore is not None
    assert indicator.to_jsonable() == before


def test_tradable_spread_uses_qff_specific_stale_threshold() -> None:
    indicator = IndicatorEngine(window=3)
    for spread in (1.0, 2.0, 3.0):
        indicator.update(bar(spread))
    observed_at = ts("2026-06-18T09:00:00+08:00")

    fresh_enough = estimate_tradable_spreads(
        LiveQuoteSet(
            qff=quote("qff", "2026-06-18T08:59:06+08:00", 100.0, bid=99.0, ask=101.0),
            tsm=quote("tsm", "2026-06-18T08:59:59+08:00", 20.0, bid=19.5, ask=20.5),
            usdttwd=quote("usd", "2026-06-18T08:59:59+08:00", 30.0, bid=29.9, ask=30.1),
        ),
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )
    stale_qff = estimate_tradable_spreads(
        LiveQuoteSet(
            qff=quote("qff", "2026-06-18T08:59:04+08:00", 100.0, bid=99.0, ask=101.0),
            tsm=quote("tsm", "2026-06-18T08:59:59+08:00", 20.0, bid=19.5, ask=20.5),
            usdttwd=quote("usd", "2026-06-18T08:59:59+08:00", 30.0, bid=29.9, ask=30.1),
        ),
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )

    assert fresh_enough.short_spread is not None
    assert fresh_enough.long_spread is not None
    assert stale_qff.short_spread is None
    assert stale_qff.long_spread is None
    assert stale_qff.missing_reason == "stale_qff"


def test_tradable_spread_keeps_tsm_and_usdttwd_at_global_stale_threshold() -> None:
    indicator = IndicatorEngine(window=3)
    for spread in (1.0, 2.0, 3.0):
        indicator.update(bar(spread))
    observed_at = ts("2026-06-18T09:00:00+08:00")

    stale_tsm = estimate_tradable_spreads(
        LiveQuoteSet(
            qff=quote("qff", "2026-06-18T08:59:30+08:00", 100.0, bid=99.0, ask=101.0),
            tsm=quote("tsm", "2026-06-18T08:59:49+08:00", 20.0, bid=19.5, ask=20.5),
            usdttwd=quote("usd", "2026-06-18T08:59:59+08:00", 30.0, bid=29.9, ask=30.1),
        ),
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )
    stale_usd = estimate_tradable_spreads(
        LiveQuoteSet(
            qff=quote("qff", "2026-06-18T08:59:30+08:00", 100.0, bid=99.0, ask=101.0),
            tsm=quote("tsm", "2026-06-18T08:59:59+08:00", 20.0, bid=19.5, ask=20.5),
            usdttwd=quote("usd", "2026-06-18T08:59:49+08:00", 30.0, bid=29.9, ask=30.1),
        ),
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )

    assert stale_tsm.missing_reason == "stale_tsm"
    assert stale_usd.missing_reason == "stale_usdttwd"


def test_tradable_spread_requires_bid_ask_but_mid_can_forward_fill_qff() -> None:
    indicator = IndicatorEngine(window=3)
    for spread in (1.0, 2.0, 3.0):
        indicator.update(bar(spread))
    observed_at = ts("2026-06-18T09:00:10+08:00")
    quote_set = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T08:59:00+08:00", 105.0, bid=104.0, ask=106.0),
        tsm=quote("tsm", "2026-06-18T09:00:10+08:00", 20.0, bid=19.5, ask=20.5),
        usdttwd=quote("usd", "2026-06-18T09:00:10+08:00", 30.0, bid=29.9, ask=30.1),
    )

    snapshot = estimate_tradable_spreads(
        quote_set,
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )

    assert snapshot.mid_spread == pytest.approx((120.0 - 100.0) / (120.0 + 100.0) * 200.0)
    assert snapshot.short_spread is None
    assert snapshot.long_spread is None

    missing_book = LiveQuoteSet(
        qff=quote("qff", "2026-06-18T09:00:10+08:00", 100.0, ask=101.0),
        tsm=quote("tsm", "2026-06-18T09:00:10+08:00", 20.0, bid=19.5, ask=20.5),
        usdttwd=quote("usd", "2026-06-18T09:00:10+08:00", 30.0, bid=29.9, ask=30.1),
    )
    missing_snapshot = estimate_tradable_spreads(
        missing_book,
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )
    assert missing_snapshot.short_spread is not None
    assert missing_snapshot.long_spread is None
    assert missing_snapshot.missing_reason == "missing_book"


def test_tradable_spread_treats_qff_diagnostic_quote_as_stale_qff() -> None:
    indicator = IndicatorEngine(window=3)
    for spread in (1.0, 2.0, 3.0):
        indicator.update(bar(spread))
    observed_at = ts("2026-06-18T09:00:10+08:00")
    qff_diagnostic_quote = LiveQuote(
        source="fubon_qff",
        symbol="QFF",
        timestamp=observed_at,
        price=100.0,
        raw={"book_missing": True},
    )

    snapshot = estimate_tradable_spreads(
        LiveQuoteSet(
            qff=qff_diagnostic_quote,
            tsm=quote("tsm", "2026-06-18T09:00:10+08:00", 20.0, bid=19.5, ask=20.5),
            usdttwd=quote("usd", "2026-06-18T09:00:10+08:00", 30.0, bid=29.9, ask=30.1),
        ),
        observed_at,
        indicator,
        stale_seconds=10.0,
        qff_book_stale_seconds=55.0,
        last_qff_close=100.0,
    )

    assert snapshot.short_spread is None
    assert snapshot.long_spread is None
    assert snapshot.missing_reason == "stale_qff"


def test_live_paper_cli_flags_default_on_and_can_disable_ui_or_color() -> None:
    parser = build_parser()

    defaults = parser.parse_args(["live-paper", "--config", "configs/live.example.toml"])
    assert not defaults.quiet_ui
    assert not defaults.no_color
    assert not defaults.skip_warmup

    disabled = parser.parse_args(
        [
            "live-paper",
            "--config",
            "configs/live.example.toml",
            "--quiet-ui",
            "--no-color",
            "--skip-warmup",
        ]
    )
    assert disabled.quiet_ui
    assert disabled.no_color
    assert disabled.skip_warmup
