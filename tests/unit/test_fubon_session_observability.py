from __future__ import annotations

from datetime import datetime, timezone

from lux_trader.store import SQLiteStore


def test_store_round_trips_latest_fubon_session_health(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "live.sqlite3")
    observed_at = datetime(2026, 7, 22, 6, 30, tzinfo=timezone.utc)
    try:
        store.initialize()
        store.record_fubon_session_health(
            observed_at=observed_at,
            health={
                "role": "trading",
                "generation": 3,
                "worker_pid": 4321,
                "status": "invalid",
                "last_login_at": observed_at,
                "last_success_at": observed_at,
                "relogin_count": 2,
                "invalid_reason": "Fubon session event 304",
            },
        )
        store.commit()

        health = store.load_latest_fubon_session_health()

        assert health is not None
        assert health["generation"] == 3
        assert health["worker_pid"] == 4321
        assert health["status"] == "invalid"
        assert health["relogin_count"] == 2
        assert health["invalid_reason"] == "Fubon session event 304"
    finally:
        store.close()
