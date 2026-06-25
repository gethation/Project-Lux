# Project Lux Architecture Refactor Plan

## Summary

Project Lux already covers replay, live-paper, live-dry-run, read-only reconciliation,
and live execution preparation. The next refactor goal is to reduce top-level module
sprawl without changing trading behavior, SQLite schema, persisted JSON payloads, or
CLI command semantics.

The refactor is intentionally staged. Each stage should keep deterministic tests
passing before moving to the next layer.

## Dependency Direction

```text
core
  <- market_data / execution / reconciliation
  <- integrations / persistence
  <- runtime
  <- cli / ui
```

Rules:

- `core/` must not import CLI, runtime, SQLite, or external broker APIs.
- `market_data/` contains provider-neutral data types, replay, warmup, parsing, and
  minute-bar logic.
- `integrations/` contains Fubon, Binance, BitoPro, and TAIFEX adapters.
- `execution/` contains execution intent, outcome, price policy, simulation, real
  coordinator, recorder, and live-execution gate.
- `reconciliation/` contains read-only broker snapshot models, fake/read-only broker
  protocol, reconciliation service, and post-trade reconciliation.
- `persistence/` owns SQLite schema and query helpers. `SQLiteStore` remains the
  single public facade.

## Completed

### 1. Config relocation

Completed on 2026-06-25.

- Created `configs/`.
- Moved `config.example.toml` to `configs/replay.example.toml`.
- Moved `config.live.example.toml` to `configs/live.example.toml`.
- Moved ignored local `*.local.toml` configs under `configs/`.
- Updated config loading so project-local relative paths still resolve from the
  project root, not from `configs/`.
- Updated README, phase report, smoke tests, and command examples.

### 2. Core domain extraction

Completed on 2026-06-25.

- Moved models, strategy, indicator, calendar, sizing, fees, tradable spread, and
  contract policy into `lux_trader/core/`.
- Added shared time and contract parsing helpers.
- Added architecture tests to keep `core/` independent from runtime, persistence,
  CLI, and external broker APIs.

### 3. Market data and integrations

Completed on 2026-06-25.

- Split provider-neutral market data modules into `lux_trader/market_data/`.
- Moved Fubon, Binance, BitoPro, and TAIFEX implementations into
  `lux_trader/integrations/`.
- Consolidated Fubon authentication, response parsing, and contract identity logic.
- Kept live/replay behavior unchanged.

### 4. Execution, reconciliation, and persistence

Completed on 2026-06-25.

- Moved execution intent, outcome, price policy, simulation, recorder, real
  coordinator, and live execution gate into `lux_trader/execution/`.
- Split reconciliation into models, broker protocol/fake broker, reconciler service,
  and post-trade reconciliation under `lux_trader/reconciliation/`.
- Kept compatibility wrappers for older import paths such as
  `lux_trader.execution_intent` and `lux_trader.live_execution_gate`.
- Moved SQLite DDL into `lux_trader/persistence/schema.py`.
- Moved execution query helpers into `lux_trader/persistence/execution_queries.py`.
- Moved reconciliation query helpers into
  `lux_trader/persistence/reconciliation_queries.py`.
- Kept `SQLiteStore` as the single public persistence facade.
- Kept SQLite table definitions and persisted JSON payload shapes unchanged.

## Remaining

### 5. Live runtime split

Completed on 2026-06-25.

- Split live bootstrap, warmup, contract switching, mode handlers, and polling engine
  out of `live_runner.py`.
- Added `lux_trader/runtime/live/bootstrap.py`.
- Added `lux_trader/runtime/live/warmup.py`.
- Added `lux_trader/runtime/live/contracts.py`.
- Added `lux_trader/runtime/live/modes.py`.
- Added `lux_trader/runtime/live/engine.py`.
- Kept `live-paper`, `live-dry-run`, and `live-execute` on a shared runtime engine.
- Kept `lux_trader/live_runner.py` as a compatibility re-export module.

### 6. CLI split

- Split parser, dispatch, and command implementations out of `cli.py`.
- Keep public entry point `python -m lux_trader` unchanged.

### 7. Test and documentation cleanup

- Group tests by unit/integration/smoke when the module layout is stable.
- Keep smoke tests gated by env vars.
- Remove compatibility wrappers only after internal imports and docs no longer depend
  on old paths.

## Validation

Run after each stage:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' env list
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest -q
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader --help
```
