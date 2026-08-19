# Project Lux

A pairs-trading system for a single spread: **UMC** (NYSE ADR, via Interactive
Brokers) against **CCF** (UMC single-stock futures on TAIFEX, via Fubon). Both
legs are the same underlying on two venues in two currencies, so the equity leg
is converted with a USD/TWD reference rate from Twelve Data.

```
umc_twd_fair = umc_price × usd_twd / 5      # one ADR is five ordinary shares
spread       = (umc_twd_fair − ccf) / (umc_twd_fair + ccf) × 200
```

The spread is a normalized percentage, not a difference. The strategy takes a
position when its rolling z-score is far from the mean and closes when it comes
back.

**Entries are scored on the executable spread, not the mid.** `short_spread`
prices the round trip you would actually get selling UMC and buying CCF (UMC bid
+ CCF ask); `long_spread` is the other direction. `long ≥ mid ≥ short` always
holds, and the gap between them is the execution cost the signal has to clear.

| Position | Opens when | Closes when |
| --- | --- | --- |
| Short UMC / long CCF | `short_z > +entry_z` | `long_z < −exit_z` |
| Long UMC / short CCF | `long_z < −entry_z` | `short_z > +exit_z` |

The decision z-score flips to the closing side once a position is open, so
`exit_z` is not symmetric with `entry_z` in effect — raising it delays every
exit rather than taking profit sooner.

The pair trades only where both venues are open: Taipei **21:30–04:00** in US
summer, 22:30–05:00 in US winter.

## Safety model

`live --mode execute` is the only entrypoint that sends real orders, and it is
held shut by four independent locks plus three environment gates:

1. `safety.allow_live_order = true`
2. `[live_execution] enabled = true`
3. `[live_execution] ccf_first = true` — the futures leg goes first, because a
   stranded CCF position can be closed in its own session while a stranded UMC
   short accrues borrow every day it is open
4. `require_readonly_reconciliation = true` — a fresh, matched read-only
   reconciliation against both brokers at every startup
5. `PROJECT_LUX_ALLOW_LIVE_ORDER`, `FUBON_ALLOW_LIVE_ORDER`,
   `IBKR_ALLOW_LIVE_ORDER` set to `1` (done for you by `scripts/lux.ps1`)

Everything else fails closed. A stale quote, a skewed pair of legs, or a
position the brokers and the store disagree about produces a skipped minute or a
refused startup, never a trade on a guess.

**Run it attended.** See [docs/HANDOFF.md](docs/HANDOFF.md).

## Setup

Python 3.12 in a Conda environment named `Quant`:

```powershell
conda create -n Quant python=3.12
conda activate Quant
pip install -r requirements.txt -r requirements-dev.txt
```

The Fubon SDK is not on PyPI — install the vendor wheel separately. Broker
credentials live in `.env` (git-ignored); point `fubon_env_path` at it.

`scripts/lux.ps1` finds Conda itself, so no path needs hardcoding.

## Usage

### Replay — deterministic, no broker contact

```powershell
.\scripts\lux.ps1 replay  --config configs/replay.fixture.ccf_umc.toml --reset-store
.\scripts\lux.ps1 summary --config configs/replay.fixture.ccf_umc.toml
```

Pinned to the committed fixture under `tests/fixtures/replay/`: 12,460 bars, 18
trades, net 235,726.66 TWD, 20,134.41 TWD of fees. `tests/integration/test_replay_golden.py`
asserts those to the last digit and is the tripwire for any change to the maths.

That golden pins the **strategy**, not live behaviour: replay scores on the mid
z-score and fills on the next bar, while live scores on the directional spread
and fills on the same bar. A green golden does not mean live would take these
trades.

### Dry run — full live rehearsal, simulated fills

```powershell
.\scripts\lux.ps1 live --mode dry-run --config configs/config.live.ccf_umc.dryrun.local.toml --reset-store
```

Real market data, real warmup, real reconciliation, `DRYRUN-*` orders. Touches
no order API.

### Live execute — real orders

```powershell
.\scripts\lux.ps1 live --mode execute --config configs/config.live.ccf_umc.execute.local.toml --resume
```

`--resume` carries an open position across a restart; `--reset-store` starts
clean and must never be used while a position is open. Ctrl+C shuts down
cleanly — brokers closed, lease released.

### Web viewer

A read-only chart of the spread, the executable band, both directional
z-scores, and entry/exit markers. `live` starts one automatically on port 8787;
to run it alone:

```powershell
.\scripts\lux-web.ps1 --config configs/config.live.ccf_umc.execute.local.toml --port 8787
```

It opens the store `mode=ro` and can never write to it. Binding anything other
than loopback mints a required token — plain HTTP with no TLS, so keep it on a
LAN or VPN.

| Variable | Effect |
| --- | --- |
| `LUX_NO_WEB=1` | do not start the viewer with `live` |
| `LUX_WEB_HOST` | bind address (default `127.0.0.1`) |
| `LUX_WEB_PORT` | port (default `8787`) |
| `LUX_WEB_TOKEN` | supply a token instead of generating one |

### Status and recovery

```powershell
# Read-only broker reconciliation — no orders, safe any time
$env:LUX_READONLY_BROKER='1'
.\scripts\lux.ps1 status reconcile --config <cfg> --readonly

.\scripts\lux.ps1 status doctor  --config <cfg> --mode live
.\scripts\lux.ps1 status live    --config <cfg>
.\scripts\lux.ps1 status broker  --config <cfg> [--funds | --orders SYMBOL]
.\scripts\lux.ps1 status margin  --config <cfg>
```

`admin exec-smoke` places one minimal real order per venue and
`admin manual-close` flattens a single stranded leg. Both need their own extra
env gates; `manual-close` is close-only and refuses anything that would grow or
flip a position.

When a leg is closed by hand outside the system, `recover manual-flat` squares
the ledger without inventing fill prices — which leaves the round trip's PnL
unbooked and `pnl_status` pending. `recover settle-manual-close` books it:

```powershell
.\scripts\lux.ps1 recover settle-manual-close --config <cfg> --from-broker --readonly
```

A hand-placed order's executions are not reachable over the API — IBKR returns
executions only to the client that placed them, unless that client holds the
Gateway's master client id — so this settles on the account's realized PnL
instead, which IBKR reports net of real commissions. That figure covers the
current trading day only; after the nightly reset, pass `--umc-exit-price` from
your own statement. Supplying both cross-checks one against the other and
refuses if they disagree. Dry-run by default, like every other recovery
command; both it and `manual-flat` refuse while a live run holds the lease.

### Tests

```powershell
conda run -n Quant python -m pytest -q
```

## Layout

```
lux_trader/
  core/           strategy, indicators, sizing, fees, calendar — the frozen maths
  market_data/    minute bar assembly, staleness and skew gates, warmup
  execution/      execution intent, gate, coordinators, price policy
  integrations/   fubon/, ibkr/, twelvedata/, taifex/ — each broker in its own process
  reconciliation/ broker-vs-store position checks
  runtime/live/   the live loop, bootstrap, contract rollover
  persistence/    SQLite schema and migrations
  web/            read-only viewer
  cli/            command dispatch
configs/          one TOML per mode; *.local.toml hold machine-specific paths
docs/             design notes and runbooks
scripts/          PowerShell launchers
```

Every broker integration runs in its own spawned process, so a wedged SDK
thread or a hung socket has an OS-enforceable deadline instead of blocking the
trading loop.

## Docs

- [docs/CCF_UMC_PLAN.md](docs/CCF_UMC_PLAN.md) — design decisions and what was measured
- [docs/HANDOFF.md](docs/HANDOFF.md) — operating notes
- [docs/LIVE_START_COMMANDS.md](docs/LIVE_START_COMMANDS.md) — startup sequence
- [docs/MIGRATION.md](docs/MIGRATION.md) — moving to another machine
