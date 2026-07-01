# M6 — Minimal Live-Execute Acceptance (real two-leg orders)

This is the first and only step that sends **real money orders**. Run it
**attended**, with **minimal sizing (~1 QFF lot)**, during regular QFF trading
hours (day session 08:45–13:45, and not a Friday-night/close-only session).

The acceptance forces exactly one round trip: one real two-leg **entry**
(QFF + Binance TSM) then one real two-leg **exit**, and verifies the account ends
flat. It is driven by `tests/smoke/test_live_execute_smoke.py`, which is **skipped
unless every gate below is set**.

## 1. Prerequisites

- The single-leg smokes already passed (Binance TSM, Fubon TMF). ✅ (done)
- Full deterministic suite green: `conda run -n Quant pytest -q`.
- `.env` (Fubon + `BINANCE_API_KEY`/`BINANCE_SECRET`) and the Fubon `.pfx` are in
  the project root.
- **The broker account is flat** (no QFF/TSM position, no open orders). The test
  asserts this before it trades, but confirm it yourself first.
- Config `configs/config.live.exec.smoke.local.toml` exists (gitignored). It ships
  with `leg_notional_twd = 240000` (~1 QFF lot at QFF≈2400) and `allow_live_order =
  true`. **Review the sizing for your account before running.**

## 2. Pre-flight (no real orders)

Preview the exact sizing and confirm the gates without sending anything.

```powershell
# a) See the sizing this config produces (simulated fills, no real orders).
#    Copy the exec config to a *.dryrun.local.toml and set allow_live_order=false,
#    then:
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  live-dry-run --config configs/config.live.exec.dryrun.local.toml --reset-store --max-iterations 130
#    Confirm the OPEN line shows qff_contracts=1 (or your intended lots) and a
#    TSM units value you are comfortable trading.

# b) Confirm every live-order gate is OPEN (this sends no orders).
$env:PROJECT_LUX_ALLOW_LIVE_ORDER='1'; $env:FUBON_ALLOW_LIVE_ORDER='1'; $env:BINANCE_ALLOW_LIVE_ORDER='1'
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  live-order-doctor --config configs/config.live.exec.smoke.local.toml
```

`live-order-doctor` must report all gate checks PASS. If any FAIL, stop and fix it.

## 3. Run the acceptance (SENDS REAL ORDERS)

Attended, during trading hours, all five gates set:

```powershell
$env:LUX_LIVE_MARKETDATA='1'
$env:LUX_READONLY_BROKER='1'
$env:PROJECT_LUX_ALLOW_LIVE_ORDER='1'
$env:FUBON_ALLOW_LIVE_ORDER='1'
$env:BINANCE_ALLOW_LIVE_ORDER='1'

& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant `
  pytest tests/smoke/test_live_execute_smoke.py -q -s `
  -m "live_marketdata and readonly_broker and live_execute_smoke"

Remove-Item Env:\LUX_LIVE_MARKETDATA, Env:\LUX_READONLY_BROKER, `
  Env:\PROJECT_LUX_ALLOW_LIVE_ORDER, Env:\FUBON_ALLOW_LIVE_ORDER, Env:\BINANCE_ALLOW_LIVE_ORDER
```

It writes to `data\live_execute_smoke.sqlite3` (reset each run).

## 4. Pass criteria

The test asserts all of these; on a green run they are met:

- Entry: `live_execution filled` + `post_trade_reconciliation matched`; strategy
  state `open`; `orders`/`fills` present with **no `DRYRUN-*` ids** (real orders);
  latest execution outcome `filled`.
- Exit: `live_execution filled` + `post_trade_reconciliation matched`; strategy
  state `flat`; exactly 1 `trades` row.
- **Final broker check: both brokers report 0 positions and 0 open orders.**

## 5. If it stops with an open position (recovery)

A failed/partial leg pauses the strategy (`PAUSED`) and may leave a real position.
Do **not** re-run the acceptance. Recover with the M2 tools:

```powershell
# Inspect state, position, last outcome, reconciliation.
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  live-status --config configs/config.live.exec.smoke.local.toml

# Flatten each stranded leg by hand (needs the manual-close env gates).
#   Fubon (futures lot):
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  fubon-manual-close --config ... --symbol <QFFxx> --side sell --lot 1 --confirm-symbol <QFFxx>
#   Binance (TSM units):
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  binance-manual-close --config ... --symbol TSM/USDT:USDT --side buy --quantity <units> --confirm-symbol TSM/USDT:USDT

# After both legs are confirmed flat, clear the pause (re-runs reconciliation).
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader `
  clear-pause --config ... --readonly
```

## 6. On success

Record the acceptance (order ids, fill prices, PnL, final flat) in
`PROJECT_LUX_PHASE_REPORT.md`. M6 is then complete; proceed to **M7** (unattended
soak across a session incl. a contract rollover, plus the go-live runbook).
