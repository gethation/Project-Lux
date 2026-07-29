"""The UMC leg's venues, and the wall where they are not wired yet.

Phase A2 deleted Binance and BitoPro along with the rest of QFF/TSM. UMC's real
venues -- IBKR for quotes, positions and orders, Twelve Data for USD/TWD --
arrive in Phase B and Phase D of docs/CCF_UMC_PLAN.md.

Everything goes through this one module instead of importing a venue directly,
so the gap is four functions in one file rather than dead imports scattered
across the runtime. Each raises rather than returning a degraded stand-in: a
pair that quietly prices its US leg off nothing, or reports a position it never
queried, is far more dangerous than one that refuses to start.

The CCF leg is unaffected -- Fubon is wired and stays wired.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from ..config import AppConfig


class UsLegVenueNotWired(RuntimeError):
    """Raised when the runtime asks for a UMC venue that Phase B/D has not built."""


def _refuse(what: str, phase: str) -> "UsLegVenueNotWired":
    return UsLegVenueNotWired(
        f"The UMC leg has no {what}. Binance/BitoPro were removed in Phase A2 "
        f"and the IBKR/Twelve Data replacement lands in {phase} "
        f"(see docs/CCF_UMC_PLAN.md). Inject a provider explicitly to run "
        f"anything that needs one before then."
    )


def open_umc_quote_provider(config: "AppConfig") -> Any:
    """Live UMC quotes. Phase B: IBKR via a subprocess-isolated ib_async client."""
    raise _refuse("quote provider", "Phase B")


def open_fx_quote_provider(config: "AppConfig") -> Any:
    """Live USD/TWD. Phase B: Twelve Data behind a TTL cache.

    A real USD/TWD rate, NOT the USDT/TWD one BitoPro serves. Those are
    different rates: BitoPro's was measured at 77% of spread std against
    CCF/UMC, and it drifts inside the z window, so the rolling z-score cannot
    absorb it. Deriving a synthetic USD/TWD from it was measured too and came
    out worse on every metric -- the wedge is Taiwan's local crypto premium, not
    something a currency ratio cancels.
    """
    raise _refuse("FX reference provider", "Phase B")


def open_umc_readonly_broker(symbol: str, env_path: Path | None = None) -> Any:
    """Read-only UMC positions/balances. Phase B: IBKR."""
    raise _refuse("read-only broker", "Phase B")


def open_umc_execution_adapter(symbol: str, env_path: Path | None = None) -> Any:
    """UMC order placement. Phase D: IBKR.

    The largest single piece of new code in the plan -- nothing anywhere in the
    repo can place a US-leg order today.
    """
    raise _refuse("execution adapter", "Phase D")


def fetch_umc_market_time(symbol: str) -> Any:
    """An independent market clock for the startup clock-skew gate.

    Binance's USD-M server time used to serve this. It is a safety gate --
    clock_skew_fail_seconds refuses to start the loop on a drifting clock -- so
    it raises here rather than quietly returning the local clock, which would
    make every skew check pass by construction.

    Phase B picks the replacement: IBKR server time, or NTP. It need not be the
    US venue at all; any clock that is not this machine's will do.
    """
    raise _refuse("market clock for the skew gate", "Phase B")


__all__ = [
    "UsLegVenueNotWired",
    "fetch_umc_market_time",
    "open_fx_quote_provider",
    "open_umc_execution_adapter",
    "open_umc_quote_provider",
    "open_umc_readonly_broker",
]
