"""SNTP client for the startup clock-skew gate.

The gate exists because the live loop labels minute bars off the LOCAL clock,
and staleness is ``local_now - quote.timestamp`` against three feeds that each
stamp in their own real time. A drifting local clock therefore breaks every
comparison at once, mislabels bars, and writes wrong timestamps to the store.

So the reference has to be absolute time, not a venue's time:

* All three feeds are themselves disciplined to real time. Checking against one
  venue would check that venue's clock, not ours.
* A venue probe cannot run when the venue is down -- and the IB Gateway has a
  daily login screen, so a Gateway-based gate would fail exactly at startup, for
  a reason unrelated to clock health. The gate fails closed, so that becomes
  "cannot start".
* Windows already syncs from NTP (``w32tm /resync`` in the same preflight).
  Verifying with NTP makes the two halves agree: sync from NTP, then confirm the
  sync took. Verifying against a venue would check a different thing than the
  one we just adjusted to.

Implemented on the stdlib rather than a package: SNTP is one 48-byte UDP
exchange, and this repo keeps five dependencies.
"""

from __future__ import annotations

import socket
import struct
import time
from datetime import UTC, datetime

from ..core.time import TAIPEI_TZ


# Seconds between the NTP epoch (1900-01-01) and the Unix epoch.
NTP_EPOCH_OFFSET = 2_208_988_800
NTP_PORT = 123
NTP_PACKET_SIZE = 48

# First byte: leap indicator 0, version 3, mode 3 (client).
_CLIENT_PACKET = b"\x1b" + 47 * b"\0"

# Taiwan's national time service first -- lowest round trip from a Taipei host
# and locally authoritative -- then the global pool, so a network that blocks
# one still has a route. Measured 2026-07-29: 29-59ms round trip, all reachable.
DEFAULT_NTP_SERVERS = (
    "time.stdtime.gov.tw",
    "tock.stdtime.gov.tw",
    "pool.ntp.org",
)
DEFAULT_NTP_TIMEOUT_SECONDS = 5.0


class NtpUnavailable(RuntimeError):
    """No configured NTP server answered, so skew cannot be verified."""


def query_ntp_server(
    server: str,
    *,
    timeout_seconds: float = DEFAULT_NTP_TIMEOUT_SECONDS,
    socket_factory=None,
    monotonic=time.monotonic,
) -> datetime:
    """One SNTP exchange, returned as a Taipei-stamped datetime.

    The server's transmit timestamp describes when IT sent the reply, so half
    the round trip has already elapsed by the time we read it. Adding rtt/2 is
    the standard estimate. It is worth microseconds against a 60-second gate,
    but getting it wrong in the other direction is free to avoid.
    """
    factory = socket_factory or (
        lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    )
    sock = factory()
    try:
        sock.settimeout(timeout_seconds)
        started = monotonic()
        sock.sendto(_CLIENT_PACKET, (server, NTP_PORT))
        data, _ = sock.recvfrom(NTP_PACKET_SIZE)
        round_trip = monotonic() - started
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if len(data) < NTP_PACKET_SIZE:
        raise NtpUnavailable(f"{server} returned {len(data)} bytes, expected 48")
    stratum = data[1]
    if stratum == 0:
        # Stratum 0 is a "kiss of death": the server is refusing, and bytes
        # 40-47 are not a timestamp. Reading them anyway would yield a plausible
        # but meaningless instant.
        raise NtpUnavailable(f"{server} replied with a kiss-of-death packet")

    seconds, fraction = struct.unpack("!II", data[40:48])
    if seconds == 0:
        raise NtpUnavailable(f"{server} returned a zero transmit timestamp")
    unix_seconds = seconds - NTP_EPOCH_OFFSET + fraction / 2**32
    return datetime.fromtimestamp(
        unix_seconds + round_trip / 2.0, UTC
    ).astimezone(TAIPEI_TZ)


def fetch_reference_time(
    servers=DEFAULT_NTP_SERVERS,
    *,
    timeout_seconds: float = DEFAULT_NTP_TIMEOUT_SECONDS,
    query=query_ntp_server,
) -> datetime:
    """First server that answers wins.

    Deliberately not a quorum. A wrong answer cannot cause bad trading here: the
    caller fails closed, so a bogus time refuses startup rather than letting the
    loop run on a bad clock. Cross-checking servers would buy availability, not
    safety, and cost a round trip on every start.
    """
    names = tuple(servers)
    if not names:
        raise NtpUnavailable("No NTP servers configured")
    failures: list[str] = []
    for server in names:
        try:
            return query(server, timeout_seconds=timeout_seconds)
        except Exception as exc:
            failures.append(f"{server}:{type(exc).__name__}")
    raise NtpUnavailable(
        "No NTP server answered (" + ", ".join(failures) + ")"
    )


__all__ = [
    "DEFAULT_NTP_SERVERS",
    "DEFAULT_NTP_TIMEOUT_SECONDS",
    "NTP_EPOCH_OFFSET",
    "NtpUnavailable",
    "fetch_reference_time",
    "query_ntp_server",
]
