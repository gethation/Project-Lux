from __future__ import annotations

import os
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from lux_trader.integrations.fubon.market_data_process import (
    FubonMarketDataWorkerTimeout,
    FubonCcfMarketDataProcess,
)


# These two tests hang the first worker on purpose, then require the REPLACEMENT
# worker to come up inside the same budget. On Windows the replacement is a
# spawn, so it re-imports the module chain from scratch -- measured at 1.89s
# against the 2.0s this used to allow. That 0.11s of headroom is why they went
# red under load: the budget was measuring the machine's import speed, not the
# rebuild it means to test. 6.0s clears the measured cost with room to spare.
WORKER_REBUILD_TIMEOUT_SECONDS = 6.0


def _send_ok(connection: Connection, result: Any = None) -> None:
    connection.send(
        {
            "ok": True,
            "result": result,
            "candidate_session_counts": {},
            "candidate_session_summaries": {},
        }
    )


def _first_init_hangs_worker(
    connection: Connection,
    marker: Path | None,
    _book_wait_timeout_seconds: float,
) -> None:
    assert marker is not None
    try:
        while True:
            request = connection.recv()
            operation = request["operation"]
            if operation == "connect" and not marker.exists():
                marker.write_text(str(os.getpid()), encoding="utf-8")
                time.sleep(30.0)
                continue
            if operation == "fetch_candidates":
                _send_ok(connection, [{"symbol": "CCFH6"}])
            else:
                _send_ok(connection)
            if operation == "close":
                return
    except (EOFError, BrokenPipeError, OSError):
        return


def _reconnect_hangs_worker(
    connection: Connection,
    marker: Path | None,
    _book_wait_timeout_seconds: float,
) -> None:
    assert marker is not None
    first_worker = not marker.exists()
    if first_worker:
        marker.write_text(str(os.getpid()), encoding="utf-8")
    try:
        while True:
            request = connection.recv()
            operation = request["operation"]
            if operation == "reconnect" and first_worker:
                time.sleep(30.0)
                continue
            _send_ok(connection)
            if operation == "close":
                return
    except (EOFError, BrokenPipeError, OSError):
        return


def _always_hangs_worker(
    connection: Connection,
    _marker: Path | None,
    _book_wait_timeout_seconds: float,
) -> None:
    try:
        while True:
            connection.recv()
            time.sleep(30.0)
    except (EOFError, BrokenPipeError, OSError):
        return


def test_initial_realtime_timeout_terminates_and_rebuilds_worker(tmp_path) -> None:
    marker = tmp_path / "first-worker.txt"
    provider = FubonCcfMarketDataProcess(
        marker,
        init_timeout_seconds=WORKER_REBUILD_TIMEOUT_SECONDS,
        terminate_timeout_seconds=0.5,
        worker_target=_first_init_hangs_worker,
    )
    try:
        provider.connect()

        first_pid = int(marker.read_text(encoding="utf-8"))
        assert provider.worker_pid is not None
        assert provider.worker_pid != first_pid
        assert provider.fetch_candidates("CCF") == [{"symbol": "CCFH6"}]
    finally:
        provider.close()


def test_reconnect_timeout_terminates_and_rebuilds_worker(tmp_path) -> None:
    marker = tmp_path / "first-worker.txt"
    provider = FubonCcfMarketDataProcess(
        marker,
        init_timeout_seconds=WORKER_REBUILD_TIMEOUT_SECONDS,
        terminate_timeout_seconds=0.5,
        worker_target=_reconnect_hangs_worker,
    )
    try:
        provider.connect()
        first_pid = provider.worker_pid

        provider.reconnect()

        assert first_pid is not None
        assert provider.worker_pid is not None
        assert provider.worker_pid != first_pid
    finally:
        provider.close()


def test_replacement_worker_timeout_is_bounded_and_leaves_no_worker(tmp_path) -> None:
    provider = FubonCcfMarketDataProcess(
        tmp_path / "unused.txt",
        init_timeout_seconds=1.0,
        terminate_timeout_seconds=0.5,
        worker_target=_always_hangs_worker,
    )
    started = time.monotonic()
    try:
        with pytest.raises(
            FubonMarketDataWorkerTimeout,
            match="replacement worker",
        ):
            provider.connect()
        assert time.monotonic() - started < 5.0
        assert provider.worker_pid is None
    finally:
        provider.close()
