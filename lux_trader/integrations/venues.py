"""Where the UMC leg gets its venues.

Everything goes through this one module instead of importing a venue directly,
so the wiring is one file to read rather than scattered across the runtime, and
so the parts that are still missing say which phase fills them in.

Wired (Phase B): UMC quotes and history from IBKR, USD/TWD from Twelve Data
behind a TTL cache, read-only IBKR account access.

Not wired: UMC order placement, which needs the IBKR execution adapter in Phase
D. It raises rather than returning a degraded stand-in -- a pair that quietly
prices its US leg off nothing, or reports a position it never queried, is far
more dangerous than one that refuses to start.

The CCF leg is unaffected: Fubon is wired and stays wired.

The ib_async and requests imports are deliberately function-local. `replay` and
`summary` never touch a venue, and they must keep working on a machine with no
IB Gateway and no brokerage packages installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from ..config import AppConfig


class UsLegVenueNotWired(RuntimeError):
    """Raised when the runtime asks for a UMC venue that no phase has built."""


def _refuse(what: str, phase: str) -> "UsLegVenueNotWired":
    return UsLegVenueNotWired(
        f"The UMC leg has no {what}. It arrives in {phase} "
        f"(see docs/CCF_UMC_PLAN.md). Inject one explicitly to run anything "
        f"that needs it before then."
    )


def open_umc_quote_provider(config: "AppConfig") -> Any:
    """Live UMC quotes from IBKR, over a subprocess-isolated ib_async client."""
    from .ibkr.market_data import IbkrUmcQuoteProvider

    return IbkrUmcQuoteProvider(
        host=config.live.ibkr_host,
        port=config.live.ibkr_port,
        client_id=config.live.ibkr_client_id,
        market_data_type=config.live.ibkr_market_data_type,
    )


def open_fx_quote_provider(config: "AppConfig") -> Any:
    """USD/TWD from Twelve Data, throttled to the free tier's credit budget.

    A real USD/TWD rate, NOT the USDT/TWD one BitoPro serves. Those are
    different rates: BitoPro's was measured at 77% of spread std against
    CCF/UMC, and it drifts inside the z window, so the rolling z-score cannot
    absorb it. Deriving a synthetic USD/TWD from it was measured too and came
    out worse on every metric -- the wedge is Taiwan's local crypto premium, not
    something a currency ratio cancels.

    The cache is applied here rather than inside the provider so that how stale
    a rate may be stays one visible decision instead of a vendor detail.
    """
    from ..market_data.cached_quote import CachedQuoteProvider
    from .twelvedata.market_data import TwelveDataMarketData

    return CachedQuoteProvider(
        TwelveDataMarketData(),
        ttl_seconds=config.live.fx_cache_ttl_seconds,
        max_serve_seconds=config.live.fx_max_serve_seconds,
    )


def open_umc_readonly_broker(symbol: str, env_path: Path | None = None) -> Any:
    """Read-only UMC positions, orders and account values from IBKR.

    Takes its own client id so it can coexist with the quote subscription: one
    Gateway session per client id, and sharing one would have the account query
    fight the streaming ticker.
    """
    from .ibkr.readonly import IbkrReadOnlyBroker

    return IbkrReadOnlyBroker()


def open_umc_execution_adapter(symbol: str, env_path: Path | None = None) -> Any:
    """UMC order placement. Phase D: IBKR.

    The largest single piece of new code in the plan -- nothing anywhere in the
    repo can place a US-leg order today.
    """
    raise _refuse("execution adapter", "Phase D")


# The startup clock-skew gate does NOT live here. It checks absolute time, not
# a venue's, so it belongs with integrations/ntp.py -- see that module for why.


__all__ = [
    "UsLegVenueNotWired",
    "open_fx_quote_provider",
    "open_umc_execution_adapter",
    "open_umc_quote_provider",
    "open_umc_readonly_broker",
]
