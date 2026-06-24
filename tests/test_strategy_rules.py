from __future__ import annotations

from lux_trader.models import Direction
from lux_trader.strategy import should_exit


def test_exit_z_one_requires_crossing_to_opposite_side() -> None:
    assert not should_exit(0.0, Direction.SHORT_TSM_LONG_QFF, 1.0)
    assert not should_exit(-0.99, Direction.SHORT_TSM_LONG_QFF, 1.0)
    assert should_exit(-1.01, Direction.SHORT_TSM_LONG_QFF, 1.0)

    assert not should_exit(0.0, Direction.LONG_TSM_SHORT_QFF, 1.0)
    assert not should_exit(0.99, Direction.LONG_TSM_SHORT_QFF, 1.0)
    assert should_exit(1.01, Direction.LONG_TSM_SHORT_QFF, 1.0)
