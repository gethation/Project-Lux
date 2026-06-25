from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from ..config import ContractPolicyConfig
from .contracts import parse_contract_expiry, row_get, row_to_dict
from .time import TAIPEI_TZ


@dataclass(frozen=True)
class QffContractSelection:
    symbol: str
    expiry: date
    raw: dict[str, Any]
    business_days_to_expiry: int


class ExpiryBufferContractPolicy:
    def __init__(self, config: ContractPolicyConfig) -> None:
        self.config = config
        self.holidays = {item for item in config.holidays}

    def select_active(
        self,
        candidates: list[Any],
        *,
        product: str,
        now: datetime | None = None,
    ) -> QffContractSelection:
        now = ensure_policy_time(now)
        parsed: list[QffContractSelection] = []
        rejected: list[str] = []
        for candidate in candidates:
            raw = row_to_dict(candidate)
            symbol = str(row_get(raw, "symbol", "code", "id", "ticker") or "").strip()
            if not symbol:
                rejected.append(str(raw))
                continue
            product_value = str(row_get(raw, "product", "productCode", "name") or symbol)
            if product.upper() not in product_value.upper() and product.upper() not in symbol.upper():
                continue
            expiry = parse_contract_expiry(raw, product)
            if expiry is None:
                rejected.append(symbol)
                continue
            remaining = business_days_between(now.date(), expiry, self.holidays)
            if remaining >= self.config.min_business_days_to_expiry:
                parsed.append(
                    QffContractSelection(
                        symbol=symbol,
                        expiry=expiry,
                        raw=raw,
                        business_days_to_expiry=remaining,
                    )
                )

        if not parsed:
            raise RuntimeError(
                "Unable to select QFF active contract with expiry buffer. "
                f"Rejected candidates: {rejected[:10]}"
            )
        return sorted(parsed, key=lambda item: (item.expiry, item.symbol))[0]

    def force_exit_deadline(self, expiry: date) -> datetime:
        day = previous_business_day(
            expiry,
            days=self.config.force_exit_business_days_before_expiry,
            holidays=self.holidays,
        )
        hour, minute = parse_hhmm(self.config.force_exit_time)
        return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=TAIPEI_TZ)

    def should_force_exit(self, now: datetime, expiry: date | None) -> bool:
        if expiry is None:
            return False
        return ensure_policy_time(now) >= self.force_exit_deadline(expiry)


def ensure_policy_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(TAIPEI_TZ)
    if value.tzinfo is None:
        return value.replace(tzinfo=TAIPEI_TZ)
    return value.astimezone(TAIPEI_TZ)


def parse_hhmm(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.split(":", 1)
    return int(hour_text), int(minute_text)


def is_business_day(value: date, holidays: set[date]) -> bool:
    return value.weekday() < 5 and value not in holidays


def business_days_between(start: date, end: date, holidays: set[date]) -> int:
    if end <= start:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if is_business_day(current, holidays):
            count += 1
        current += timedelta(days=1)
    return count


def previous_business_day(value: date, *, days: int, holidays: set[date]) -> date:
    current = value
    remaining = days
    while remaining > 0:
        current -= timedelta(days=1)
        if is_business_day(current, holidays):
            remaining -= 1
    return current
