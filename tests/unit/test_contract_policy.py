from __future__ import annotations

from datetime import date, datetime

import pytest

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
            force_exit_time="13:35",
            holidays=(),
        )
    )


def test_business_days_between_excludes_today_and_includes_expiry() -> None:
    assert business_days_between(date(2026, 7, 8), date(2026, 7, 15), set()) == 5
    assert business_days_between(date(2026, 7, 9), date(2026, 7, 15), set()) == 4


@pytest.mark.parametrize(
    ("now", "expected_symbol", "expected_business_days"),
    (
        ("2026-07-08T09:00:00+08:00", "QFFG6", 5),
        ("2026-07-09T09:00:00+08:00", "QFFH6", 29),
    ),
)
def test_expiry_buffer_selects_contract_for_business_day_distance(
    now: str,
    expected_symbol: str,
    expected_business_days: int,
) -> None:
    selected = make_policy().select_active(
        [
            {"symbol": "QFFG6", "endDate": "2026-07-15"},
            {"symbol": "QFFH6", "endDate": "2026-08-19"},
        ],
        product="QFF",
        now=datetime.fromisoformat(now),
    )

    assert selected.symbol == expected_symbol
    assert selected.business_days_to_expiry == expected_business_days


def test_force_exit_deadline_is_previous_business_day_at_1335() -> None:
    policy = make_policy()

    assert policy.force_exit_deadline(date(2026, 7, 15)) == datetime.fromisoformat(
        "2026-07-14T13:35:00+08:00"
    )
    assert not policy.should_force_exit(
        datetime.fromisoformat("2026-07-14T13:34:59+08:00"),
        date(2026, 7, 15),
    )
    assert policy.should_force_exit(
        datetime.fromisoformat("2026-07-14T13:35:00+08:00"),
        date(2026, 7, 15),
    )
