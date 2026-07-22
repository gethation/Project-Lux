from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from lux_trader.runner import SystemRunner
from lux_trader.store import SQLiteStore

from conftest import make_app_config


GOLDEN_SUMMARY = (
    Path(__file__).parents[1] / "fixtures" / "replay" / "golden_summary.json"
)


def _assert_matches_golden(actual: Any, expected: Any, path: str = "summary") -> None:
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected a mapping"
        assert actual.keys() == expected.keys(), f"{path}: keys differ"
        for key, expected_value in expected.items():
            _assert_matches_golden(actual[key], expected_value, f"{path}.{key}")
        return

    if isinstance(expected, float):
        assert isinstance(actual, float), f"{path}: expected a float"
        assert math.isclose(actual, expected, rel_tol=1e-9, abs_tol=0.0), (
            f"{path}: {actual!r} != {expected!r}"
        )
        return

    assert type(actual) is type(expected), f"{path}: types differ"
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_fixture_replay_matches_golden_summary(tmp_path) -> None:
    config = make_app_config(tmp_path)
    result = SystemRunner(config).replay(reset_store=True)

    store = SQLiteStore(config.store_path)
    try:
        store.initialize()
        actual = store.build_summary(config.strategy, config.fees)
    finally:
        store.close()

    expected = json.loads(GOLDEN_SUMMARY.read_text(encoding="utf-8"))
    assert result.rows_processed == expected["rows"]
    _assert_matches_golden(actual, expected)
