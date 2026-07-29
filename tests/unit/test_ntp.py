"""The clock-skew gate's time source.

Every test here drives a fake socket. Nothing reaches the network: a safety
gate whose tests depend on a reachable NTP server is a gate whose tests go red
for reasons that have nothing to do with the gate.
"""

from __future__ import annotations

import socket
import struct
from datetime import UTC, datetime

import pytest

from lux_trader.integrations.ntp import (
    DEFAULT_NTP_SERVERS,
    NTP_EPOCH_OFFSET,
    NtpUnavailable,
    fetch_reference_time,
    query_ntp_server,
)


def ntp_packet(moment: datetime, *, stratum: int = 2) -> bytes:
    """A well-formed 48-byte reply carrying ``moment`` as the transmit stamp."""
    unix_seconds = moment.timestamp()
    seconds = int(unix_seconds) + NTP_EPOCH_OFFSET
    fraction = int((unix_seconds % 1) * 2**32)
    packet = bytearray(48)
    packet[0] = 0x1C  # LI 0, VN 3, mode 4 (server)
    packet[1] = stratum
    packet[40:48] = struct.pack("!II", seconds, fraction)
    return bytes(packet)


class FakeSocket:
    def __init__(self, reply: bytes | Exception) -> None:
        self.reply = reply
        self.timeout: float | None = None
        self.sent_to: tuple | None = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendto(self, _packet: bytes, address: tuple) -> None:
        self.sent_to = address

    def recvfrom(self, _size: int):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, ("127.0.0.1", 123)

    def close(self) -> None:
        self.closed = True


def test_reads_the_transmit_timestamp_and_returns_taipei() -> None:
    moment = datetime(2026, 7, 29, 5, 51, 24, tzinfo=UTC)
    sock = FakeSocket(ntp_packet(moment))

    result = query_ntp_server(
        "time.example",
        socket_factory=lambda: sock,
        monotonic=iter([0.0, 0.0]).__next__,
    )

    assert result.tzinfo is not None
    assert str(result.tzinfo) == "Asia/Taipei"
    assert abs((result - moment).total_seconds()) < 0.001
    assert sock.sent_to == ("time.example", 123)
    assert sock.closed


def test_adds_half_the_round_trip() -> None:
    """The server stamped when IT replied; half the trip has passed since."""
    moment = datetime(2026, 7, 29, 5, 51, 24, tzinfo=UTC)
    sock = FakeSocket(ntp_packet(moment))

    result = query_ntp_server(
        "time.example",
        socket_factory=lambda: sock,
        monotonic=iter([10.0, 10.4]).__next__,
    )

    assert (result - moment).total_seconds() == pytest.approx(0.2, abs=1e-6)


def test_socket_is_closed_even_when_the_read_fails() -> None:
    sock = FakeSocket(socket.timeout("timed out"))

    with pytest.raises(socket.timeout):
        query_ntp_server("time.example", socket_factory=lambda: sock)

    assert sock.closed


def test_kiss_of_death_is_refused_rather_than_parsed() -> None:
    """Stratum 0 means the server is refusing; bytes 40-47 are not a timestamp.

    Parsing them anyway would yield a plausible-looking instant, and a
    plausible-looking instant is exactly what this gate must never invent.
    """
    moment = datetime(2026, 7, 29, 5, 51, 24, tzinfo=UTC)
    sock = FakeSocket(ntp_packet(moment, stratum=0))

    with pytest.raises(NtpUnavailable, match="kiss-of-death"):
        query_ntp_server("time.example", socket_factory=lambda: sock)


def test_short_reply_is_refused() -> None:
    sock = FakeSocket(b"\x1c\x02" + 10 * b"\0")

    with pytest.raises(NtpUnavailable, match="expected 48"):
        query_ntp_server("time.example", socket_factory=lambda: sock)


def test_zero_transmit_timestamp_is_refused() -> None:
    """Reading it as-is would land in 1900 and merely look like huge skew."""
    packet = bytearray(ntp_packet(datetime(2026, 7, 29, tzinfo=UTC)))
    packet[40:48] = struct.pack("!II", 0, 0)
    sock = FakeSocket(bytes(packet))

    with pytest.raises(NtpUnavailable, match="zero transmit timestamp"):
        query_ntp_server("time.example", socket_factory=lambda: sock)


def test_falls_through_to_the_next_server() -> None:
    moment = datetime(2026, 7, 29, 5, 51, 24, tzinfo=UTC)
    asked: list[str] = []

    def query(server: str, *, timeout_seconds: float):
        asked.append(server)
        if server == "first.example":
            raise OSError("unreachable")
        return moment

    result = fetch_reference_time(
        ("first.example", "second.example", "third.example"),
        query=query,
    )

    assert result == moment
    # Stops at the first success: a quorum would buy availability, not safety,
    # since the caller fails closed on a bad answer anyway.
    assert asked == ["first.example", "second.example"]


def test_all_servers_failing_names_every_one() -> None:
    def query(server: str, *, timeout_seconds: float):
        raise OSError("unreachable")

    with pytest.raises(NtpUnavailable) as excinfo:
        fetch_reference_time(("a.example", "b.example"), query=query)

    message = str(excinfo.value)
    assert "a.example:OSError" in message
    assert "b.example:OSError" in message


def test_an_empty_server_list_is_refused() -> None:
    with pytest.raises(NtpUnavailable, match="No NTP servers configured"):
        fetch_reference_time(())


def test_defaults_lead_with_taiwans_national_time_service() -> None:
    # Lowest round trip from a Taipei host and locally authoritative, with the
    # global pool behind it so a network blocking one still has a route.
    assert DEFAULT_NTP_SERVERS[0].endswith(".stdtime.gov.tw")
    assert "pool.ntp.org" in DEFAULT_NTP_SERVERS
