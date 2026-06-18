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
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader warmup-live --config config.live.example.toml --reset-store
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader live-paper --config config.live.example.toml --resume
```

`live-paper` polls QFF, Binance TSM, and USDT/TWD quotes once per second, but only
evaluates the strategy after a completed minute. QFF active-symbol selection uses the
expiry buffer policy: choose the earliest QFF contract with at least 5 business days
to expiry; if an old contract is already open, keep it until an exit signal or the
T-1 13:35 force-exit safety valve. It still uses `PaperBroker`; no live order path
exists in this phase. Set `LUX_LIVE_MARKETDATA=1` before `live-doctor` only when you
intentionally want a real market-data smoke test.

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
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader qff-warmup-check --config config.live.smoke.local.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest tests/test_live_smoke.py -q -m live_marketdata
Remove-Item Env:\LUX_LIVE_MARKETDATA
```

The live smoke path logs into Fubon marketdata, resolves the expiry-buffer active QFF
contract, reads Fubon 1m candles, downloads TAIFEX previous-30-trading-day CSV ZIP files into
`data\taifex_cache`, fetches Binance `TSM/USDT:USDT` and BitoPro `USDT/TWD`, then runs
`warmup-live` through `WarmupRunner`. `qff-warmup-check` can be used alone to validate
the Fubon + TAIFEX QFF leg before touching Binance/BitoPro. Passing criteria are 1440
`warmup_bars` and zero `bars`, `orders`, `fills`, or `trades`.

## Safety

This milestone has no live trading path. `doctor` fails if live trading is enabled in
the config. Future live order code must require explicit environment and config gates.
