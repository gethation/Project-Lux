from __future__ import annotations

from datetime import date, datetime

from lux_trader.config import ContractPolicyConfig
from lux_trader.core.contract_policy import (
    ExpiryBufferContractPolicy,
    business_days_between,
)


def make_policy() -> ExpiryBufferContractPolicy:
    return ExpiryBufferContractPolicy(
        ContractPolicyConfig(
            enabled=True,
            min_business_days_to_expiry=5,
            force_exit_business_days_before_expiry=1,
            force_exit_grace_minutes=5,
            holidays=(),
        )
    )


def test_business_days_between_excludes_today_and_includes_expiry() -> None:
    assert business_days_between(date(2026, 7, 8), date(2026, 7, 15), set()) == 5
    assert business_days_between(date(2026, 7, 9), date(2026, 7, 15), set()) == 4


def test_expiry_buffer_selects_front_contract_when_buffer_is_satisfied() -> None:
    selected = make_policy().select_active(
        [
            {"symbol": "CCFG6", "endDate": "2026-07-15"},
            {"symbol": "CCFH6", "endDate": "2026-08-19"},
        ],
        product="CCF",
        now=datetime.fromisoformat("2026-07-08T09:00:00+08:00"),
    )

    assert selected.symbol == "CCFG6"
    assert selected.business_days_to_expiry == 5


def test_expiry_buffer_switches_to_next_contract_when_front_has_four_days_left() -> None:
    selected = make_policy().select_active(
        [
            {"symbol": "CCFG6", "endDate": "2026-07-15"},
            {"symbol": "CCFH6", "endDate": "2026-08-19"},
        ],
        product="CCF",
        now=datetime.fromisoformat("2026-07-09T09:00:00+08:00"),
    )

    assert selected.symbol == "CCFH6"


def test_force_exit_deadline_lands_before_the_pair_session_close() -> None:
    """Not a TAIFEX wall clock -- the pair's own session close.

    The inherited 13:35 sat in the TAIFEX day session, which CCF/UMC never
    trades. Firing there would close the CCF leg while NYSE was shut and leave
    UMC naked overnight: a defect that cannot exist in a QFF/TSM system, whose
    US leg trades around the clock and can always be closed alongside.
    """
    policy = make_policy()

    # US summer: RTH closes at Taipei 04:00, so the deadline is 03:55.
    assert policy.force_exit_deadline(date(2026, 7, 15)) == datetime.fromisoformat(
        "2026-07-14T03:55:00+08:00"
    )
    assert not policy.should_force_exit(
        datetime.fromisoformat("2026-07-14T03:54:59+08:00"),
        date(2026, 7, 15),
    )
    assert policy.should_force_exit(
        datetime.fromisoformat("2026-07-14T03:55:00+08:00"),
        date(2026, 7, 15),
    )


def test_force_exit_deadline_follows_us_dst() -> None:
    """In US winter the whole window shifts an hour later in Taipei."""
    policy = make_policy()

    assert policy.force_exit_deadline(date(2026, 1, 21)) == datetime.fromisoformat(
        "2026-01-20T04:55:00+08:00"
    )


def test_force_exit_deadline_never_lands_in_the_taifex_day_session() -> None:
    """The property the old 13:35 violated, checked across a year of expiries."""
    policy = make_policy()

    for month in range(1, 13):
        deadline = policy.force_exit_deadline(date(2026, month, 20))
        minute_of_day = deadline.hour * 60 + deadline.minute
        # TAIFEX day session is 08:45-13:45; the pair's window never overlaps it.
        assert not (8 * 60 + 45) <= minute_of_day <= (13 * 60 + 45), deadline
