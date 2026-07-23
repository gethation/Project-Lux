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
