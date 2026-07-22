from __future__ import annotations

from pathlib import Path

import pytest

from lux_trader.runtime.live.lease import (
    LiveLeaseError,
    LiveProcessLease,
    assert_live_lease_available,
)


def test_live_lease_blocks_competing_fubon_login_and_releases(tmp_path: Path) -> None:
    store_path = tmp_path / "live.sqlite3"
    lease = LiveProcessLease(store_path)
    lease.acquire(metadata={"mode": "test"})
    try:
        with pytest.raises(LiveLeaseError, match="direct Fubon login is blocked"):
            assert_live_lease_available(store_path)
        with pytest.raises(LiveLeaseError, match="Another live-execute"):
            LiveProcessLease(store_path).acquire()
    finally:
        lease.release()

    assert_live_lease_available(store_path)
