# Project Lux

QFF/TSM pairs-trading system — a simplified rebuild of the legacy Project Lux.
The core trading mechanism (strategy, indicators, sizing, fees, execution intent,
reconciliation, broker integrations, SQLite audit schema) is vendored **unchanged**
from the legacy reference implementation; only the operational shell (CLI, live
runtime, terminal UI, config) is rebuilt to reduce complexity.

Source of truth for the mechanism:
`Project Lux legacy/mechanism and requirement/01_TRADING_MECHANISM_AND_REQUIREMENTS.md`.

## Modes (target)

| Mode | Purpose | Sends real orders |
| --- | --- | --- |
| `replay` | Validate strategy against the PoC/reference dataset. | No |
| `live --mode dry-run` | Full live rehearsal on real/live-like data with a simulated adapter. | No |
| `live --mode execute` | Minimal supervised two-leg real execution behind safety gates. | Yes |

`paper` mode from the legacy system is intentionally dropped.

## Environment

Python via the Miniconda `Quant` environment:

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python --version
```

## M1 — replay alignment

Deterministic replay is pinned to the committed fixture under
`tests/fixtures/replay/` (self-contained; independent of the mutable PoC
workspace).

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader replay  --config configs/replay.fixture.toml --reset-store
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader summary --config configs/replay.fixture.toml
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant pytest -q
```

Aligned reference summary: `rows=29909`, `trade_count=66`,
`net_pnl_twd≈261507.83`, `total_fee_twd≈68317.50`.

> Note: the mechanism doc quotes an older PoC summary (`net_pnl=265481.32`). The
> PoC rebuilt its input window on 2026-06-29, permanently dropping early QFF opens;
> the trade set is unchanged (66 trades) and sizing/fill logic is byte-for-byte
> identical. The committed fixture is the frozen regression baseline.

## M2 — live dry-run + terminal UI

`live --mode dry-run` runs the full live rehearsal (auto warmup, 1s polling, minute
finalization, tradable bid/ask spread decisions, simulated `DRYRUN-*`
execution, reconciliation, resume) without touching any real order API.

```powershell
.\scripts\lux.ps1 live --mode dry-run --config configs/live.example.toml --reset-store
.\scripts\lux.ps1 live --mode dry-run --config configs/live.example.toml --resume
```

Terminal UI styles (acceptance fields: session, symbols, latest quote/bar,
spread/z-score, state, position, latest decision, reconciliation/gate):

- `--ui compact` (default): legacy single-line reporter.
- `--ui dashboard`: rich multi-panel live dashboard.
- `--quiet-ui`: no live UI, final summary only (CI/logs).

Operator commands (no real orders):

```powershell
& conda run -n Quant python -m lux_trader status doctor --config configs/live.example.toml --mode live
& conda run -n Quant python -m lux_trader status live --config <cfg>
& conda run -n Quant python -m lux_trader status reconcile --config <cfg> --readonly   # needs LUX_READONLY_BROKER=1
& conda run -n Quant python -m lux_trader recover clear-pause --config <cfg> --readonly         # only after matched reconciliation
& conda run -n Quant python -m lux_trader warmup --config <cfg> --reset-store
& conda run -n Quant python -m lux_trader summary --config <cfg> --execution
```

Real-API smokes are opt-in via env gates (`LUX_LIVE_MARKETDATA=1`,
`LUX_READONLY_BROKER=1`); the default `pytest -q` run never touches external
APIs (gated smoke tests skip).

## M3 — real execution + M6 two-leg smoke

`live --mode execute` is the only real-order entrypoint. It requires
`safety.allow_live_order=true`, `[live_execution] enabled=true`, the three
`*_ALLOW_LIVE_ORDER=1` env gates, and a matched read-only reconciliation.
At every startup (including resume), it refreshes reconciliation through the
read-only Fubon and Binance adapters before evaluating the order gate or
creating the real execution runner. A manual read-only preview remains
available:

```powershell
$env:LUX_READONLY_BROKER='1'
& conda run -n Quant python -m lux_trader status reconcile --config <cfg> --readonly
```

Single-venue tools (real orders, extra env gates required):

```powershell
& conda run -n Quant python -m lux_trader admin exec-smoke   --config <cfg> --venue fubon   --symbol FITMN07 --lot 1 --confirm-symbol FITMN07
& conda run -n Quant python -m lux_trader admin exec-smoke   --config <cfg> --venue binance --quantity 0.02 --confirm-symbol TSM/USDT:USDT
& conda run -n Quant python -m lux_trader admin manual-close --config <cfg> --venue fubon   --symbol FITMN07 --side sell --lot 1 --confirm-symbol FITMN07
& conda run -n Quant python -m lux_trader status broker --config <cfg> [--funds | --orders SYMBOL]
```

Fubon fill confirmation is layered (official status enum: 10=New Order,
50=Fully filled, 30=Cancel, 90=Failed): the SDK's futures report callbacks
(`set_on_futopt_filled` / `set_on_futopt_order`) are the primary channel,
`get_order_results` polling runs in parallel as backup, position-delta is only
a post-timeout fallback, and post-trade reconciliation remains the final
check. Outcome payloads record `fill_source`
(`filled_callback | order_result | position_delta`), `fill_events`, and
`callback_stream_unreliable` for audit.

Binance fill confirmation mirrors the same layering: every order carries a
pre-assigned `newClientOrderId` so a create_order timeout can be resolved by
`origClientOrderId` lookup (found → normal outcome, confirmed absent → FAILED,
lookup unavailable → position-delta evidence, else UNKNOWN); `fetch_order`
runs as a bounded polling loop (working statuses keep polling, terminal
statuses exit fast); position-delta is the last-resort fill evidence. Payloads
record `fill_source`, `client_order_id`, `recovery`, and `poll_errors`.

The M6 two-leg minimal real-order acceptance (Fubon TMF `FITMN07` 1 lot +
Binance TSM 0.1 unit, qff-first, pre/post reconciliation, leg timing gap,
final flat) is driven by `tests/smoke/test_live_execute_smoke.py` and MUST be
run supervised — see [docs/M6_RUNBOOK.md](docs/M6_RUNBOOK.md). Until M6 (and a
longer soak) passes, the system must not run unattended with real orders.

## Layout

```text
lux_trader/
  core/            strategy, indicator, sizing, fees, calendar, contract policy, models (frozen)
  market_data/     replay input, minute bars, warmup, sessions (frozen)
  execution/       execution intent, price policy, coordinator, gate, outcome (frozen)
  reconciliation/  read-only + post-trade reconciliation (frozen)
  integrations/    Fubon, Binance, BitoPro, TAIFEX adapters (frozen)
  persistence/     SQLite schema + query stores (frozen)
  brokers/         PaperBroker used by replay accounting (frozen)
  runtime/live/    shared live engine + dry-run/execute handlers (paper mode removed)
  store.py         single SQLite facade (frozen)
  config.py        TOML config loader (frozen)
  runner.py        replay orchestration (frozen)
  cli/             thin command shell (rebuilt, consolidated)
  terminal_ui.py   compact one-line live reporter (legacy style)
  dashboard_ui.py  rich multi-panel live dashboard
tests/             unit + integration + gated smoke + fixtures
configs/           example + fixture configs
```

## Margin management — daily 10:00 transfer guidance

Semi-automatic dual-account margin policy (source: PoC
`docs/margin_management_analysis.md`): the system reads both accounts' equity
and maintenance margin (Fubon `query_margin_equity`; Binance `/fapi/v3/account`
via ccxt), computes equity/notional ratios, and reports transfer guidance in
the terminal UI. It never places orders or initiates transfers.

- Daily check at 10:00 Taipei (Mon-Fri) inside the live loop, plus a red-line
  check every 15 minutes while a position is open. Enable with
  `[margin_management] enabled = true` and `LUX_READONLY_BROKER=1`.
- Levels: `red_line` (close immediately — a transfer arrives too late),
  `transfer` (initiate today's 10:00 transfer back to the 30% target),
  `rebalance` (flat, top up opportunistically), `ok`.
- Standalone check (also schedulable via Windows Task Scheduler):

```powershell
$env:LUX_READONLY_BROKER='1'
.\scripts\lux.ps1 status margin --config <cfg>
```

- Every check is recorded in the `margin_checks` SQLite table for later
  calibration; the dashboard shows a dedicated Margin panel.
