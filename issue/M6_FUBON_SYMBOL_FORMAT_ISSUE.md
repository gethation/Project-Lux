# M6 Fubon Symbol Format Issue

## Summary

M6 real-order smoke originally used `FITMN07` as the Fubon TMF symbol. During live M6 execution, Fubon rejected the first leg before any fill:

```text
Fubon place_order failed: Symbol month part error
```

No Binance leg was submitted, and no position was opened. The failure was not related to fill confirmation, `status=10`, or `position_delta`; it happened at Fubon `place_order`.

Root cause: Fubon market/account/reporting symbols are not always valid `FutOptOrder.symbol` values for the trading API. The live order path must use Fubon's order symbol format.

## Observed Failure

M6 output:

```text
[M6] entry execution status=failed message=live execution stopped after FUBON_QFF
Fubon place_order failed: RuntimeError: Fubon place_order failed: Symbol month part error
symbol: FITMN07
```

Fubon log showed the rejected order was submitted as:

```text
FutOptOrder {
  market_type: FutureNight,
  buy_sell: Buy,
  symbol: "FITMN07",
  price_type: Market,
  lot: 1,
  time_in_force: IOC,
  order_type: Auto
}
```

## Symbol Formats

Fubon `FutOptOrder.symbol` should use the order format shown in Fubon Trade API examples, such as `TXFD4`:

```text
<product><month_code><year_digit>
```

For 2026-07:

```text
TMF -> TMFG6
QFF -> QFFG6
```

`G` is the July month code in this format, and `6` is the final digit of 2026.

The problematic or non-order formats include:

```text
FITMN07
QFF202607
FITM + expiry_date=202607
FIQFF + expiry_date=202607
```

These can appear in market-data selection, account positions, order records, or legacy local records, but they should not be passed directly to Fubon `place_order`.

## Correct Runtime Rule

Use Fubon order symbols in execution and operator-facing commands:

```text
TMF smoke order symbol: TMFG6
QFF active order symbol: QFFG6
```

When Fubon returns account/order rows in broker-specific formats, the code should treat equivalent rows as matching the expected order symbol:

```text
Expected: TMFG6
Fubon raw row: FITM + expiry_date=202607
Meaning: same 2026-07 TMF contract

Expected: QFFG6
Fubon raw row: FIQFF + expiry_date=202607
Meaning: same 2026-07 QFF contract
```

`FITMN07` is now treated only as a legacy alias for parsing/matching old records, not as a symbol to use for live orders.

## Fixes Applied

### M6 Smoke

Updated M6 smoke to use:

```toml
[live_execution_smoke]
fubon_symbol = 'TMFG6'
qff_expiry = '202607'
```

Updated:

- `configs/config.live.exec.smoke.local.toml`
- `configs/config.live.exec.smoke.readonly.local.toml`
- `configs/config.live.exec.dryrun.local.toml`
- `tests/smoke/test_live_execute_smoke.py`
- `docs/M6_RUNBOOK.md`

### Main QFF Flow

The main QFF flow already supports automatic month selection when:

```toml
[live_market_data]
qff_symbol = 'auto'
qff_product = 'QFF'
```

The issue was that the selected market-data symbol could be passed downstream as the execution symbol. A normalization layer was added so selected symbols are converted to Fubon order symbols before they reach the live execution path.

Examples:

```text
QFF202607 -> QFFG6
FIQFFN07  -> QFFG6
FITMN07   -> TMFG6
```

Updated:

- `lux_trader/integrations/fubon/contracts.py`
- `lux_trader/runtime/live/contracts.py`

Added/updated tests:

- `tests/unit/test_fubon_execution.py`
- `tests/integration/test_live_market_data.py`

## Verification

Post-fix test results:

```text
294 passed, 8 skipped
```

Execution/reconciliation focused tests:

```text
47 passed
```

Fubon readonly checks before retrying M6:

```text
Fubon account funds:
- positions=0
- open_orders=0
- available=58,562 TWD

Fubon order records: symbol=TMFG6
- position=0
- open_orders=0
- order_records=0
```

Broker reconciliation:

```text
status=matched
issues=0
```

Order gate doctor:

```text
Live execution gate status=open
all checks PASS
```

## Operational Notes

For M6, use:

```text
TMFG6
```

Do not use:

```text
FITMN07
```

For the main QFF strategy, keep:

```toml
qff_symbol = 'auto'
```

The program will select the active month and normalize it to the Fubon order format, such as:

```text
QFFG6
QFFH6
QFFI6
```

If a live run fails after Fubon submission, do not immediately rerun M6. First check broker positions and open orders:

```powershell
$env:LUX_READONLY_BROKER='1'
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant `
  python -m lux_trader broker-status `
  --config configs/config.live.exec.smoke.readonly.local.toml `
  --orders TMFG6
```

Avoid running multiple Fubon readonly account queries in parallel; it can trigger Fubon system throttling:

```text
業務系統流量控管
```

