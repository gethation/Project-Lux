# Project Lux

MVP pairs-trading architecture for replaying the QFF/TSM proof of concept through a
single-process loop, a paper broker, and a SQLite state store.

## Project Background

Project Lux is the deployable implementation track for a QFF/TSM pairs-trading idea
that was first validated in the proof-of-concept workspace:

```text
D:\Users\Documents\Proof of Concept
```

The PoC is the source of truth for the first version of the strategy logic: trading
targets, spread construction, rolling z-score behavior, entry/exit thresholds, sizing,
fees, QFF trading-calendar assumptions, and the reference backtest summary. Project
Lux should preserve that behavior unless a change is explicitly designed, documented,
and revalidated against the PoC reference.

This repository focuses on the production shape around that validated logic:
configuration, broker abstraction, market-data adapters, paper execution, SQLite
state recovery, CLI workflows, deterministic tests, and live market-data smoke tests.
The Phase 1 acceptance test is therefore not "does the idea look profitable again";
it is "does the Project Lux replay match the PoC trade count, direction, position,
PnL, and fees."

The first milestone intentionally does not use Fubon or Binance live APIs. It reads
the PoC CSV, recomputes the rolling z-score, runs the strategy state machine, records
paper orders/fills/trades, and supports resume from SQLite.

## Environment

This machine uses Miniconda. Run Python commands through the `Quant` environment:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python --version
```

For interactive live commands in PowerShell, prefer the project wrapper. It uses
the `Quant` environment and streams output with `conda run --no-capture-output`:

```powershell
.\scripts\lux.ps1 live-dry-run --config config.live.smoke.local.toml --reset-store
```

Install test tooling:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pip install -r requirements-dev.txt
```

## Commands

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader doctor --config config.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader replay --config config.example.toml --reset-store
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader summary --config config.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest
```

Phase 2 live market data with paper orders:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config config.live.example.toml
.\scripts\lux.ps1 live-paper --config config.live.example.toml --reset-store
.\scripts\lux.ps1 live-paper --config config.live.example.toml --resume
```

`live-paper` is the normal Phase 2 system entrypoint. On startup it checks whether the
SQLite store already has enough seed bars for the selected QFF contract; if not, it
auto-runs the live warmup flow before polling quotes. Use `--skip-warmup` only when
you explicitly want startup to fail unless existing seed bars are already present.
`warmup-live` is still available as a debug/acceptance tool for manually rebuilding
seed bars without starting the live loop:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader warmup-live --config config.live.example.toml --reset-store
```

After startup, `live-paper` polls QFF, Binance TSM, and USDT/TWD quotes once per
second, but only evaluates the strategy after a completed minute. QFF active-symbol
selection uses the expiry buffer policy: choose the earliest QFF contract with at
least 5 business days to expiry; if an old contract is already open, keep it until an
exit signal or the T-1 13:35 force-exit safety valve. It still uses `PaperBroker`; no
live order path exists in this phase. Set `LUX_LIVE_MARKETDATA=1` before `live-doctor`
only when you intentionally want a real market-data smoke test.

By default, `live-paper` shows a compact terminal UI: same-minute `LIVE` snapshots
refresh on one line, finalized `BAR` rows and warnings/events are printed on separate
lines. Use `--quiet-ui` to disable it or `--no-color` to keep the UI without ANSI
colors. Live entry/exit signals use bid/ask-adjusted tradable spreads while `mid`
remains available as the PoC/reference spread. Unlike replay/backtest, live modes
execute immediately after the finalized minute confirms the signal:

```text
09:12:04 LIVE mid=1.84 shortSpread(spread=1.62,z=1.51) longSpread(spread=2.06,z=1.93) FLAT
09:14 BAR  mid=2.24 z=2.06 shortSpread(spread=2.18,z=2.00) longSpread(spread=2.31,z=2.17) OPEN entry_fill pnl=-550 eq=999,450
```

Live runtime uses `[trading_calendar].closed_dates` for manually configured market
holidays. During a closed date or a non-trading session, the live loop does not fetch
quotes, does not finalize BAR rows, and only refreshes a yellow countdown line:

```text
02:31:04 LIVE non-trading session next=06/22 08:45 in=54:13:56
```

## Warmup-live testing

Run deterministic warmup tests without external APIs:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' env list
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_market_data.py -q
```

Run real market-data smoke tests only when `.env` and the Fubon certificate are present
in the project root. `config.live.smoke.local.toml` is intentionally ignored by git and
writes to `data\warmup_smoke.sqlite3`.

```powershell
$env:LUX_LIVE_MARKETDATA='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader qff-warmup-check --config config.live.smoke.local.toml --output-csv=
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_smoke.py -q -m live_marketdata
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

The live smoke path logs into Fubon marketdata, resolves the expiry-buffer active QFF
contract, reads Fubon 1m candles, downloads TAIFEX previous-30-trading-day CSV ZIP files into
`data\taifex_cache`, fetches Binance `TSM/USDT:USDT` and BitoPro `USDT/TWD`, then runs
`warmup-live` through `WarmupRunner`. `qff-warmup-check` can be used alone to validate
the Fubon + TAIFEX QFF leg before touching Binance/BitoPro. Passing criteria are 1440
`warmup_bars` and zero `bars`, `orders`, `fills`, or `trades`.

The full startup smoke in `tests/test_live_smoke.py` uses
`data\live_paper_startup_smoke.sqlite3`: it starts `live-paper` from an empty store,
expects `warmup_auto start/done_1440`, polls real quotes long enough to finalize or
skip a minute with a recorded warning, then runs a second `--resume` style pass and
checks that warmup is not rebuilt.

## Broker Reconciliation Skeleton

Phase 3 starts with a read-only broker reconciliation skeleton that does not touch
Fubon or Binance private APIs. Use fake brokers to validate the local data flow:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader broker-doctor --config config.live.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config config.live.example.toml --fake
```

`reconcile-brokers --fake` writes a reconciliation report to SQLite. Mismatches are
recorded as `warning` and do not block `live-paper` in this phase.

After `.env` contains Fubon credentials plus `BINANCE_API_KEY` / `BINANCE_SECRET`, run
real read-only smoke tests explicitly:

```powershell
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader broker-doctor --config config.live.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config config.live.example.toml --readonly
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_readonly_brokers_smoke.py -q -m readonly_broker
Remove-Item Env:\LUX_READONLY_BROKER
```

## Dry-run And Phase 5 Extension Point

Phase 4 dry-run execution uses the same execution pipeline shape planned for live
orders, but with a simulated adapter. It records the pair execution plan, simulates
full fills, writes simulated `DRYRUN-*` orders/fills, and updates strategy state just
like a real execution outcome would. No Fubon or Binance order API is called.

```powershell
.\scripts\lux.ps1 dry-run-doctor --config config.live.example.toml
.\scripts\lux.ps1 live-dry-run --config config.live.example.toml --reset-store
.\scripts\lux.ps1 execution-summary --config config.live.example.toml
```

Full dry-run validation has two layers. The default deterministic suite must pass
without touching external APIs:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest -q
```

The real API smoke requires the ignored `config.live.smoke.local.toml`, Fubon
credentials, Binance read-only keys, and explicit gates. It writes to
`data\live_dry_run_full_smoke.sqlite3`. Accepted dry-run entry plans should create
simulated `DRYRUN-*` orders/fills and move the strategy to `OPEN`; simulated exit
plans close the position and write the trade/PnL record. `PAUSED` is reserved for
rejected, failed, partial, or unknown execution outcomes.

```powershell
$env:LUX_LIVE_MARKETDATA='1'
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-doctor --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader dry-run-doctor --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader reconcile-brokers --config config.live.smoke.local.toml --readonly
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_dry_run_smoke.py -q -m "live_marketdata and readonly_broker and dry_run_smoke"
Remove-Item Env:\LUX_LIVE_MARKETDATA
Remove-Item Env:\LUX_READONLY_BROKER
```

For a manual 10-15 minute soak, use the same smoke config:

```powershell
$env:LUX_LIVE_MARKETDATA='1'
.\scripts\lux.ps1 live-dry-run --config config.live.smoke.local.toml --reset-store --max-iterations 900 --no-color
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

Phase 5 `live-execute` now uses the same live runtime as `live-paper` and
`live-dry-run`: auto warmup, quote polling, minute finalization, tradable bid/ask
spread decisions, trading calendar, and QFF contract policy are shared. The mode
only swaps the execution layer to the real Fubon/Binance adapters and runs
post-trade read-only reconciliation after each real execution.

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-order-doctor --config config.live.example.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-execute --config config.live.example.toml --quiet-ui
```

`live-execute` is still gated by `safety.allow_live_order=true`,
`[live_execution].enabled=true`, `PROJECT_LUX_ALLOW_LIVE_ORDER=1`,
`FUBON_ALLOW_LIVE_ORDER=1`, `BINANCE_ALLOW_LIVE_ORDER=1`, and the configured
read-only reconciliation policy. Keep it disabled unless you are intentionally
running the minimal live-order acceptance path.

## Safety

`live-paper` and `live-dry-run` still refuse `allow_live_order=true` and cannot send
real orders. `live-execute` is the only live-order entrypoint, and it requires all
explicit config/env gates plus read-only broker reconciliation. Fubon TMF execution
smoke and full `live-execute` live-order acceptance must be run manually before
treating the system as ready for unattended real execution.
