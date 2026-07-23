from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lux_trader.core.indicator import IndicatorEngine
from lux_trader.core.models import MarketBar


def make_bar(index: int, spread: float) -> MarketBar:
    return MarketBar(
        row_index=index,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
        tw_leg_close=100.0,
        tw_leg_close_filled=100.0,
        us_leg_twd_fair=100.0,
        spread=spread,
        entry_allowed=True,
        close_allowed=True,
    )


def test_indicator_warmup_and_population_std() -> None:
    engine = IndicatorEngine(window=3)
    first = engine.update(make_bar(0, 1.0))
    second = engine.update(make_bar(1, 2.0))
    third = engine.update(make_bar(2, 3.0))

    assert not first.zscore_valid
    assert not second.zscore_valid
    assert third.zscore_valid
    assert third.mean == pytest.approx(2.0)
    assert third.std == pytest.approx((2.0 / 3.0) ** 0.5)
    assert third.zscore == pytest.approx((3.0 - 2.0) / ((2.0 / 3.0) ** 0.5))


def test_indicator_state_roundtrip() -> None:
    engine = IndicatorEngine(window=3)
    for index, spread in enumerate([1.0, 2.0, 3.0]):
        engine.update(make_bar(index, spread))

    restored = IndicatorEngine.from_jsonable(engine.to_jsonable())
    snapshot = restored.update(make_bar(3, 4.0))

    assert snapshot.zscore_valid
    assert list(restored.values) == [2.0, 3.0, 4.0]
