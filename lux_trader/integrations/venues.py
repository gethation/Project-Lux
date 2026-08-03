"""Where the UMC leg gets its venues.

Everything goes through this one module instead of importing a venue directly,
so the wiring is one file to read rather than scattered across the runtime, and
so the parts that are still missing say which phase fills them in.

Wired (Phase B): UMC quotes and history from IBKR, USD/TWD from Twelve Data
behind a TTL cache, read-only IBKR account access.

Everything is wired as of Phase D1/D2, though the execution adapter is a
skeleton whose short-selling paths cannot be validated until the IBKR account is
upgraded to margin. Anything that is ever unwired here raises rather than
returning a degraded stand-in -- a pair that quietly prices its US leg off
nothing, or reports a position it never queried, is far more dangerous than one
that refuses to start.

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

    The dotenv load is deliberate and belongs here rather than at the call site.
    Twelve Data is the only venue whose credential is read in THIS process --
    Fubon logs in inside its subprocess worker, and IBKR authenticates through
    the Gateway -- so nothing else loads the file on the parent's behalf.
    `status doctor --mode live` happened to work anyway because it builds an
    in-process Fubon client first and `login_fubon_sdk` loads the file as a side
    effect; the live loop builds the subprocess wrapper instead, which does not.
    That made a missing API key look like an ordering quirk between two commands
    rather than what it is.
    """
    from ..market_data.cached_quote import CachedQuoteProvider
    from .env import load_dotenv
    from .twelvedata.market_data import TwelveDataMarketData

    # Named for Fubon, but it is the one credential file this repo has.
    load_dotenv(config.live.fubon_env_path)
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
    """UMC order placement via IBKR.

    The only trade-capable IBKR session in the repo, and the only caller that
    asks IbkrClientProcess for readonly=False. It still refuses to send anything
    unless both live-order env gates are open, checked per order.

    A skeleton: exercised against a fake worker, never against a real Gateway,
    and its short-selling paths cannot be validated at all until the account is
    upgraded to margin.
    """
    from .ibkr.execution import IbkrUmcExecutionAdapter

    return IbkrUmcExecutionAdapter(symbol)


# The startup clock-skew gate does NOT live here. It checks absolute time, not
# a venue's, so it belongs with integrations/ntp.py -- see that module for why.


__all__ = [
    "UsLegVenueNotWired",
    "open_fx_quote_provider",
    "open_umc_execution_adapter",
    "open_umc_quote_provider",
    "open_umc_readonly_broker",
]
