from __future__ import annotations

from datetime import datetime
from multiprocessing.connection import Connection
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from lux_trader.integrations.ibkr.client_process import (
    IbkrClientProcess,
    IbkrConnectionConfig,
    _IbkrWorkerClient,
)
from lux_trader.integrations.subprocess_transport import SubprocessTransport


TAIPEI = ZoneInfo("Asia/Taipei")


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object) -> "FakeEvent":
        self.handlers.append(handler)
        return self

    def emit(self, *args: object) -> None:
        for handler in self.handlers:
            handler(*args)


class FakeIb:
    def __init__(self, *, fail_connects: int = 0) -> None:
        self.errorEvent = FakeEvent()
        self.connectedEvent = FakeEvent()
        self.disconnectedEvent = FakeEvent()
        self.fail_connects = fail_connects
        self.connected = False
        self.connect_calls = 0
        self.client = SimpleNamespace(serverVersion=lambda: 178)

    def connect(self, *_args: object, **_kwargs: object) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.fail_connects:
            raise ConnectionRefusedError("refused")
        self.connected = True
        self.connectedEvent.emit()

    def isConnected(self) -> bool:
        return self.connected

    def managedAccounts(self) -> list[str]:
        return ["U1234567"]

    def reqContractDetails(self, _contract: object) -> list[object]:
        contract = SimpleNamespace(
            conId=46_613_372,
            symbol="UMC",
            exchange="SMART",
            primaryExchange="NYSE",
            currency="USD",
        )
        return [
            SimpleNamespace(
                contract=contract,
                longName="UNITED MICROELECTRON-SP ADR",
                timeZoneId="US/Eastern",
                tradingHours="20260723:0400-20260723:2000",
                liquidHours="20260723:0930-20260723:1600",
            )
        ]

    def reqMarketDataType(self, _tier: int) -> None:
        return None

    def reqMktData(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            marketDataType=3,
            last=21.39,
            close=21.29,
            bid=21.38,
            ask=21.40,
            bidSize=10,
            askSize=12,
            time=fixed_clock(),
            lastTimestamp=None,
            delayedLastTimestamp=1_753_292_700,
        )

    def sleep(self, _seconds: float) -> None:
        return None

    def cancelMktData(self, _contract: object) -> None:
        return None

    def disconnect(self) -> None:
        self.connected = False
        self.disconnectedEvent.emit()


def fixed_clock() -> datetime:
    return datetime(2026, 7, 23, 20, 0, tzinfo=TAIPEI)


def test_worker_tracks_connectivity_codes_and_contract_resolution() -> None:
    fake = FakeIb()
    worker = _IbkrWorkerClient(
        IbkrConnectionConfig(client_id=17_111),
        ib_factory=lambda: fake,
        clock=fixed_clock,
    )

    health = worker.connect()
    details = worker.resolve_umc_contract()
    quote = worker.fetch_umc_quote(quote_wait_timeout_seconds=1.0)
    fake.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    lost = worker.session_health(reconnect=False)
    fake.errorEvent.emit(-1, 1101, "Connectivity restored - data lost", None)
    restored_lost = worker.session_health(reconnect=False)
    fake.errorEvent.emit(-1, 1102, "Connectivity restored - data maintained", None)
    restored = worker.session_health(reconnect=False)

    assert health["connected"] is True
    assert health["server_version"] == 178
    assert health["accounts"] == ["U1234567"]
    assert details.con_id == 46_613_372
    assert details.time_zone_id == "US/Eastern"
    assert quote["market_data_tier"] == 3
    assert quote["last"] == 21.39
    assert lost["status"] == "connectivity_lost"
    assert lost["data_lost"] is True
    assert restored_lost["status"] == "restored_data_lost"
    assert restored_lost["data_lost"] is True
    assert restored["status"] == "restored"
    assert restored["data_lost"] is False
    assert restored["last_event_at"] == "2026-07-23T20:00:00+08:00"


def test_gateway_login_screen_is_health_state_and_next_check_reconnects() -> None:
    fake = FakeIb(fail_connects=1)
    worker = _IbkrWorkerClient(
        IbkrConnectionConfig(),
        ib_factory=lambda: fake,
        clock=fixed_clock,
    )

    unavailable = worker.connect()
    recovered = worker.session_health()

    assert unavailable["connected"] is False
    assert unavailable["status"] == "gateway_unavailable"
    assert "daily login screen" in unavailable["message"]
    assert recovered["connected"] is True
    assert recovered["status"] == "connected"
    assert fake.connect_calls == 2


def fake_ibkr_worker(
    connection: Connection,
    connection_config: IbkrConnectionConfig,
) -> None:
    while True:
        request = connection.recv()
        operation = request["operation"]
        if operation == "connect":
            result = {
                "connected": True,
                "client_id": connection_config.client_id,
            }
        elif operation == "session_health":
            result = {"connected": True, "status": "connected"}
        elif operation == "close":
            connection.send({"ok": True, "result": None})
            return
        else:
            connection.send(
                {
                    "ok": False,
                    "error_type": "RuntimeError",
                    "error": f"unsupported {operation}",
                }
            )
            continue
        connection.send({"ok": True, "result": result})


def test_process_facade_reuses_subprocess_transport() -> None:
    process = IbkrClientProcess(
        client_id=17_222,
        worker_target=fake_ibkr_worker,
    )
    try:
        assert isinstance(process._transport, SubprocessTransport)
        assert process.connect() == {"connected": True, "client_id": 17_222}
        health = process.session_health()
        assert health["connected"] is True
        assert health["status"] == "connected"
        assert isinstance(health["worker_pid"], int)
    finally:
        process.close()


class FakePositionsIb(FakeIb):
    """ib_async's shape for the 2026-08-17 fault.

    `positions()` is a cache read that cannot fail. `reqPositions()` is the
    request that fills it, and the only thing that can report the failure.
    """

    def __init__(self, *, positions_timeout: bool = False) -> None:
        super().__init__()
        self.positions_timeout = positions_timeout
        self.req_positions_calls = 0
        self._cache: list[object] = []

    def reqPositions(self) -> list[object]:
        self.req_positions_calls += 1
        if self.positions_timeout:
            raise TimeoutError("positions request timed out")
        self._cache = [
            SimpleNamespace(
                account="U1234567",
                contract=SimpleNamespace(
                    symbol="UMC", secType="STK", currency="USD", conId=46_613_372
                ),
                position=396.0,
                avgCost=18.5,
            )
        ]
        return list(self._cache)

    def positions(self) -> list[object]:
        return list(self._cache)

    def openTrades(self) -> list[object]:
        return []

    def accountSummary(self) -> list[object]:
        return []


def test_a_timed_out_positions_request_is_refused_not_reported_as_flat() -> None:
    """REGRESSION 2026-08-17: ib_async logs "positions request timed out" during
    connect and connects anyway, leaving an empty cache. 396 UMC shares read as
    a position of 0, and reconciliation blamed the position rather than the
    connection."""
    from lux_trader.integrations.ibkr.client_process import IbkrPositionsUnavailable

    fake = FakePositionsIb(positions_timeout=True)
    worker = _IbkrWorkerClient(
        IbkrConnectionConfig(), ib_factory=lambda: fake, clock=fixed_clock
    )
    worker.connect()

    for call in (worker.fetch_umc_position, worker.fetch_account_snapshot):
        try:
            call()
        except IbkrPositionsUnavailable as exc:
            assert "cannot be trusted" in str(exc)
        else:  # pragma: no cover - the point of the test
            raise AssertionError(f"{call.__name__} reported an unverified cache")

    assert worker.session_health(reconnect=False)["positions_verified"] is False


def test_a_verified_session_reads_positions_and_does_not_re_request() -> None:
    """The guard must cost one request per session, not one per read --
    fetch_umc_position runs inside order confirmation."""
    fake = FakePositionsIb()
    worker = _IbkrWorkerClient(
        IbkrConnectionConfig(), ib_factory=lambda: fake, clock=fixed_clock
    )
    worker.connect()

    assert worker.fetch_umc_position() == 396.0
    assert worker.fetch_umc_position() == 396.0
    assert worker.fetch_account_snapshot()["positions"][0]["quantity"] == 396.0
    assert fake.req_positions_calls == 1
    assert worker.session_health(reconnect=False)["positions_verified"] is True


def test_a_disconnect_invalidates_the_previous_session_cache() -> None:
    """A cache filled before a drop says nothing about the session after it."""
    fake = FakePositionsIb()
    worker = _IbkrWorkerClient(
        IbkrConnectionConfig(), ib_factory=lambda: fake, clock=fixed_clock
    )
    worker.connect()
    assert worker.fetch_umc_position() == 396.0

    fake.connected = False
    fake.disconnectedEvent.emit()
    assert worker.session_health(reconnect=False)["positions_verified"] is False

    fake.connected = True
    assert worker.fetch_umc_position() == 396.0
    assert fake.req_positions_calls == 2
