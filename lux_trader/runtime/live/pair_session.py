"""Per-pair trading-session status.

QFF/TSM trades whenever TAIFEX is open, because its US leg is a 24/7 Binance
perpetual -- TAIFEX hours ARE the pair's hours. CCF/UMC's US leg is a cash
equity that only prices during NYSE RTH, so the pair's window is the
intersection: TAIFEX open AND UMC RTH. Outside it a spread built from a frozen
UMC price would be fiction, and the loop should idle the pair as non-trading
rather than spray staleness warnings for hours.

Which model applies is derived from ``us_leg.venue`` rather than a new config
field: the venue already states the structure (binance = always-open leg,
ibkr = RTH-limited leg), and a field that must agree with the venue is a field
that can silently disagree with it.

US market holidays are NOT modelled here, matching how TAIFEX holidays are
handled (an explicit closed_dates list, empty by default): on a US holiday the
pair looks open but the UMC feed serves nothing, the staleness gate skips every
minute, and no bar is built. Loud but safe.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from lux_trader.config import AppConfig
from lux_trader.core.calendar import LiveSessionStatus, live_session_status
from lux_trader.core.time import ensure_taipei
from lux_trader.integrations.ibkr.calendar import umc_rth_session


US_RTH_VENUES = {"ibkr"}


def pair_trades_us_rth_only(config: AppConfig) -> bool:
    return config.active_pair.us_leg.venue in US_RTH_VENUES


def umc_rth_status(observed_at: datetime) -> tuple[bool, datetime]:
    """(inside RTH, next RTH open) for a Taipei instant.

    A UMC session for US market date D spans Taipei D 21:30 to D+1 04:00 in
    summer (an hour later in winter), so the session containing ``observed_at``
    may belong to yesterday's US date. Both candidates are checked.
    """
    observed = ensure_taipei(observed_at)
    for offset in (1, 0):
        market_date = observed.date() - timedelta(days=offset)
        if market_date.weekday() >= 5:
            continue
        session = umc_rth_session(market_date)
        if session.opens_at <= observed < session.closes_at:
            return True, session.opens_at

    for offset in range(0, 8):
        market_date = observed.date() + timedelta(days=offset - 1)
        if market_date.weekday() >= 5:
            continue
        session = umc_rth_session(market_date)
        if session.opens_at > observed:
            return False, session.opens_at
    raise RuntimeError(f"No UMC RTH open found within a week of {observed_at}")


def filter_umc_rth_minutes(index):
    """Keep only the minutes of a Taipei DatetimeIndex that fall in UMC RTH.

    Used to build the warmup index for RTH-limited pairs: the TAIFEX session
    index is the superset, and the pair's own minutes are its intersection with
    NYSE RTH. Sessions are cached per US market date because a 14-day window is
    ~16k minutes and each date's session is a fixed pair of instants.
    """
    sessions: dict = {}

    def contains(moment: datetime) -> bool:
        for offset in (1, 0):
            market_date = moment.date() - timedelta(days=offset)
            if market_date.weekday() >= 5:
                continue
            session = sessions.get(market_date)
            if session is None:
                session = umc_rth_session(market_date)
                sessions[market_date] = session
            if session.opens_at <= moment < session.closes_at:
                return True
        return False

    return index[[contains(ts.to_pydatetime()) for ts in index]]


def pair_session_status(
    config: AppConfig,
    observed_at: datetime,
) -> LiveSessionStatus:
    """The active pair's trading-session status at ``observed_at``."""
    base = live_session_status(
        observed_at,
        config.trading_calendar.closed_dates,
    )
    if not pair_trades_us_rth_only(config):
        return base

    inside_rth, next_rth_open = umc_rth_status(observed_at)
    if base.is_trading and inside_rth:
        return base
    if base.is_trading and not inside_rth:
        observed = ensure_taipei(observed_at)
        return LiveSessionStatus(
            is_trading=False,
            is_close_only=False,
            reason="us_rth_closed",
            # Approximation: the later of "now" (TAIFEX already open) and the
            # next RTH open. TAIFEX could close again inside a long US break,
            # but this value only feeds the countdown display and the
            # non-trading event, never a trading decision.
            next_open_at=max(next_rth_open, observed),
            countdown=max(next_rth_open - observed, timedelta(0)),
        )
    # TAIFEX itself is closed. If UMC is closed too, the pair reopens at
    # whichever market opens later (same display-only approximation as above);
    # if UMC is somehow open, TAIFEX's own next open is the wait.
    if not inside_rth and next_rth_open > base.next_open_at:
        observed = ensure_taipei(observed_at)
        return LiveSessionStatus(
            is_trading=False,
            is_close_only=False,
            reason=base.reason,
            next_open_at=next_rth_open,
            countdown=max(next_rth_open - observed, timedelta(0)),
        )
    return base
