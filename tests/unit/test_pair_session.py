"""Per-pair session status: TAIFEX for QFF/TSM, TAIFEX ∩ UMC RTH for CCF/UMC.

The intersection is derived from us_leg.venue == 'ibkr', and the RTH boundary
follows US Eastern time through DST -- Taipei 21:30-04:00 in summer,
22:30-05:00 in winter. Those two facts are what these tests pin.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from conftest import make_app_config
from lux_trader.core.calendar import live_session_status
from lux_trader.runtime.live.pair_session import (
    pair_session_status,
    pair_trades_us_rth_only,
    umc_rth_status,
)


def ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture()
def qff_config(tmp_path):
    return make_app_config(tmp_path, validate_expected_zscore=False)


@pytest.fixture()
def ccf_config(qff_config):
    pair = qff_config.pairs[0]
    pair = replace(
        pair,
        id="ccf_umc",
        us_leg=replace(pair.us_leg, venue="ibkr", symbol="UMC"),
    )
    return replace(qff_config, pairs=(pair,), active_pair_id="ccf_umc")


def test_venue_derivation(qff_config, ccf_config) -> None:
    assert not pair_trades_us_rth_only(qff_config)
    assert pair_trades_us_rth_only(ccf_config)


def test_qff_pair_status_is_exactly_the_taifex_status(qff_config) -> None:
    for text in (
        "2026-07-22T22:00:00+08:00",
        "2026-07-22T14:00:00+08:00",
        "2026-07-25T12:00:00+08:00",
    ):
        moment = ts(text)
        assert pair_session_status(qff_config, moment) == live_session_status(
            moment, ()
        )


# --- UMC RTH in Taipei terms -------------------------------------------------


def test_summer_rth_is_2130_to_0400_taipei() -> None:
    # 2026-07-22 is a Wednesday; US Eastern is on DST.
    inside, _ = umc_rth_status(ts("2026-07-22T21:30:00+08:00"))
    assert inside
    inside, _ = umc_rth_status(ts("2026-07-23T03:59:00+08:00"))
    assert inside
    inside, next_open = umc_rth_status(ts("2026-07-23T04:00:00+08:00"))
    assert not inside
    assert next_open == ts("2026-07-23T21:30:00+08:00")
    inside, next_open = umc_rth_status(ts("2026-07-22T21:29:00+08:00"))
    assert not inside
    assert next_open == ts("2026-07-22T21:30:00+08:00")


def test_winter_rth_is_2230_to_0500_taipei() -> None:
    # 2026-01-21 is a Wednesday; US Eastern is on standard time.
    inside, next_open = umc_rth_status(ts("2026-01-21T21:30:00+08:00"))
    assert not inside
    assert next_open == ts("2026-01-21T22:30:00+08:00")
    inside, _ = umc_rth_status(ts("2026-01-21T22:30:00+08:00"))
    assert inside
    inside, _ = umc_rth_status(ts("2026-01-22T04:59:00+08:00"))
    assert inside
    inside, _ = umc_rth_status(ts("2026-01-22T05:00:00+08:00"))
    assert not inside


def test_us_weekend_has_no_rth() -> None:
    # Saturday 04:30 Taipei belongs to Friday's US session in summer -- that
    # one is real. Saturday evening onwards must map to Monday.
    inside, _ = umc_rth_status(ts("2026-07-25T03:00:00+08:00"))
    assert inside  # Friday 2026-07-24's session, still open
    inside, next_open = umc_rth_status(ts("2026-07-25T22:00:00+08:00"))
    assert not inside
    assert next_open == ts("2026-07-27T21:30:00+08:00")  # Monday's session


# --- the intersection --------------------------------------------------------


def test_ccf_pair_trades_only_inside_both_windows(ccf_config) -> None:
    # TAIFEX night session + UMC RTH: trading.
    assert pair_session_status(ccf_config, ts("2026-07-22T22:00:00+08:00")).is_trading
    # TAIFEX day session, US night: not trading for this pair...
    day_status = pair_session_status(ccf_config, ts("2026-07-22T09:30:00+08:00"))
    assert not day_status.is_trading
    assert day_status.reason == "us_rth_closed"
    assert day_status.next_open_at == ts("2026-07-22T21:30:00+08:00")
    # ...even though QFF/TSM would be trading at the same instant.
    assert live_session_status(ts("2026-07-22T09:30:00+08:00"), ()).is_trading


def test_ccf_pair_follows_dst_shift(ccf_config) -> None:
    # Taipei 21:45 is inside RTH in summer but before the winter open.
    assert pair_session_status(ccf_config, ts("2026-07-22T21:45:00+08:00")).is_trading
    winter = pair_session_status(ccf_config, ts("2026-01-21T21:45:00+08:00"))
    assert not winter.is_trading
    assert winter.reason == "us_rth_closed"


def test_ccf_pair_when_both_markets_closed_reports_the_later_open(
    ccf_config,
) -> None:
    # Sunday noon: TAIFEX reopens Monday 08:45, UMC Monday 21:30. The pair's
    # wait is until the later of the two.
    status = pair_session_status(ccf_config, ts("2026-07-26T12:00:00+08:00"))
    assert not status.is_trading
    assert status.next_open_at == ts("2026-07-27T21:30:00+08:00")


def test_ccf_pair_taifex_tail_after_umc_close(ccf_config) -> None:
    # Summer: UMC closes 04:00, TAIFEX night runs to 05:00. That last hour is
    # close-only in spirit but simply non-trading for this pair.
    status = pair_session_status(ccf_config, ts("2026-07-23T04:30:00+08:00"))
    assert not status.is_trading
    assert status.reason == "us_rth_closed"
