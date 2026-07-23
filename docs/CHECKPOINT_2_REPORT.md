# Checkpoint 2 Report — Phase 1 complete

Phase 1 is complete on `feature/multipair-phase` through commit `68e3838`.
Only the configured `qff_tsm` pair is active; no CCF/UMC pair or
UMC-intersection calendar was added.

Incremental implementation commits:

| Sub-task | Commit |
|---|---|
| 1.1 data-model generalization | `3afffbe` |
| 1.2 pair-scoped schema and schema-version refusal | `8e6e4f4` |
| 1.3 `[[pairs]]` configuration | `f6829bb` |
| 1.4 `SessionCalendar` protocol | `a70fbd3` |
| 1.5 per-pair fees and contract multipliers | `68e3838` |

## 1. Replay golden

`tests/integration/test_replay_golden.py` is green. The committed golden file has
no diff from Checkpoint 1. The allowed replay command was rerun after archiving the
rejected schema-v0 store:

```text
Replay complete: rows_processed=29909, start_row=0, end_row=29908, finalized=True
```

Full `summary` JSON output, verbatim:

```json
{
  "fee_defaults_as_of": "2026-06-17",
  "parameters": {
    "entry_z": 2.0,
    "exit_z": 1.0,
    "zscore_window": 500,
    "leg_notional_twd": 1000000.0,
    "qff_lots": null,
    "initial_capital_twd": 2000000.0,
    "max_entry_delay_minutes": 15,
    "tsm_fee_bps": 5.0,
    "qff_fee_per_contract_twd": 5.0,
    "qff_tax_rate": 2e-05,
    "qff_contract_multiplier": 100.0
  },
  "rows": 29909,
  "start": "2026-05-08 17:25:00+08:00",
  "end": "2026-06-22 13:44:00+08:00",
  "entry_allowed_minutes": 25037,
  "close_allowed_minutes": 29909,
  "friday_night_close_only_minutes": 4176,
  "weekend_session_close_only_minutes": 4872,
  "friday_session_end_force_close_minutes": 7,
  "qff_forward_filled_session_minutes": 6328,
  "trade_count": 66,
  "friday_session_forced_exits": 0,
  "winning_trades": 53,
  "losing_trades": 13,
  "win_rate": 0.803030303030303,
  "total_pnl_twd": 261507.82918245532,
  "gross_pnl_twd": 329825.3260614279,
  "net_pnl_twd": 261507.82918245535,
  "total_fee_twd": 68317.49687897251,
  "total_tsm_fee_twd": 62990.49687897251,
  "total_qff_fee_twd": 2720.0,
  "total_qff_tax_twd": 2607.0,
  "return_pct": 0.13075391459122765,
  "gross_profit_twd": 322026.5453590049,
  "gross_loss_twd": -60518.71617654954,
  "profit_factor": 5.321106687385204,
  "avg_trade_pnl_twd": 3962.239836097808,
  "max_drawdown_twd": -37238.95947526302,
  "max_drawdown_pct": -0.017398306667278585,
  "elapsed_minutes": 64579,
  "exposure_minutes": 29487,
  "exposure_ratio": 0.4566035398504158,
  "final_equity_twd": 2261507.8291824553
}
```

The required acceptance values remain exactly:
`rows=29909`, `trade_count=66`,
`net_pnl_twd=261507.82918245535`, and
`total_fee_twd=68317.49687897251`.

## 2. Test result

Final `pytest -q` output from the committed Phase 1 tree:

```text
........................................................................ [ 16%]
........................................................................ [ 33%]
........ssssssss........................................................ [ 49%]
........................................................................ [ 66%]
........................................................................ [ 83%]
........................................................................ [ 99%]
.                                                                        [100%]
425 passed, 8 skipped in 130.30s (0:02:10)
```

All eight gated smoke tests remain skipped. Reconciliation against Checkpoint 1:

| Change | Collected-pass effect | Running result |
|---|---:|---:|
| Checkpoint 1 | — | 410 / 8 |
| Pair/account schema scope and old-store refusal | +2 | 412 / 8 |
| Six config migrations, sizing rules, dynamic pair parser | +10 | 422 / 8 |
| `SessionCalendar` protocol seam | +1 | 423 / 8 |
| QFF and CCF multiplier cases | +2 | 425 / 8 |
| Final | +15 | **425 passed / 8 skipped** |

No test was deleted or merged in Phase 1.

## 3. Mechanical naming check

The exact §6.1.1 `grep -ric` command was run. GNU grep returned exit 1 because
there were no matches; every emitted per-file count was `0`. Aggregating those
same source-only results by required directory gives:

```text
lux_trader/core:0
lux_trader/execution:0
lux_trader/market_data:0
lux_trader/persistence:0
lux_trader/reconciliation:0
lux_trader/runtime:0
```

The expanded §6.1.1 surface (`store.py`, `config.py`, `terminal_ui.py`,
`dashboard_ui.py`, `trade_pnl.py`, and `cli/*.py`) also returned:

```text
expanded_surfaces:0
```

There are no non-zero lines to justify. Instrument-specific strings remain only
where the specification permits or requires data identity: pair configuration,
tests/fixtures, tests, documentation, and venue-specific integration adapters.
The TAIFEX downloader's product filter and session selector now receive the product
code as a parameter.

## 4. Schema diff and old-store refusal

Schema version is now `2`. The representative DDL change is:

```sql
-- Before
CREATE TABLE strategy_state (
    id INTEGER PRIMARY KEY CHECK (id = 1), ...
);
CREATE TABLE bars (
    row_index INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL UNIQUE,
    qff_close_filled REAL NOT NULL,
    tsm_twd_fair REAL NOT NULL, ...
);

-- After
CREATE TABLE pairs (
    pair_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tw_leg_display TEXT NOT NULL,
    us_leg_display TEXT NOT NULL,
    tw_leg_venue TEXT NOT NULL,
    us_leg_venue TEXT NOT NULL
);
CREATE TABLE strategy_state (
    pair_id TEXT PRIMARY KEY, ...,
    FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
);
CREATE TABLE bars (
    pair_id TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    tw_leg_close_filled REAL NOT NULL,
    us_leg_twd_fair REAL NOT NULL, ...,
    PRIMARY KEY(pair_id, row_index),
    UNIQUE(pair_id, timestamp),
    FOREIGN KEY(pair_id) REFERENCES pairs(pair_id)
);
```

All `qff_*`/`tsm_*` schema columns were renamed to `tw_leg_*`/`us_leg_*`.
The exact scope implemented and asserted by
`tests/integration/test_pair_scoped_schema.py` is:

| Scope | Tables |
|---|---|
| Pair-scoped (`pair_id NOT NULL`) | `strategy_state`, `events`, `orders`, `fills`, `positions`, `bars`, `trades`, `market_ticks`, `warmup_bars`, `execution_plans`, `execution_legs`, `execution_checks`, `execution_simulations`, `execution_outcomes`, `pending_manual_closes`, `position_adjustments`, `fubon_order_attempts`, `fubon_evidence_events` |
| Account-scoped (no `pair_id`) | `margin_checks`, `broker_reconciliation_runs`, `broker_snapshots`, `fubon_session_events` |
| Nullable attribution | `broker_reconciliation_issues.pair_id`, inferred from the issue symbol when a configured pair claims it |

The old `ensure_column`/`ALTER TABLE` upgrade path was removed. A real schema-v0
fixture store was opened read-only through the normal `summary` path and refused
before initialization with this exact message:

```text
lux_trader.persistence.schema.StoreSchemaVersionError: Project Lux store schema is incompatible (found version 0, required 2). Archive this store and create a new one; in-place migration is not supported.
```

It remained intact and was moved to
`data/archive/replay_fixture.schema-v0.20260723-093609.sqlite3` before the new
schema-v2 fixture store was created. Nothing was silently recreated or migrated.

## 5. Config migration

Before Phase 1, every config put input paths in `[paths]`, strategy and sizing
together in `[strategy]`, fees and multipliers in `[fees]`, and instrument symbols
in global live settings. After Phase 1, `[paths]` contains only the store path and
every file declares one `[[pairs]]` entry with `id = "qff_tsm"`,
`label = "QFF/TSM"`, and these pair-local tables:
`pairs.data`, `pairs.tw_leg`, `pairs.us_leg`, `pairs.fx`, `pairs.sizing`,
`pairs.strategy`, and `pairs.fees`.

Per-file before/after:

| Config | Before | After |
|---|---|---|
| `configs/config.live.exec.dryrun.local.toml` | Global paths/strategy/fees/live instrument keys; fixed lots encoded in strategy | One `qff_tsm` pair; `fixed_lots`, `lots=1`; QFF multiplier 100; pair-local QFF/TSM/FX/fees |
| `configs/config.live.exec.local.toml` | Same global shape; fixed lots encoded in strategy | One `qff_tsm` pair; `fixed_lots`, `lots=1`; QFF multiplier 100; pair-local QFF/TSM/FX/fees |
| `configs/config.live.smoke.local.toml` | Global replay/live data, strategy, and fee tables | One `qff_tsm` pair; explicit `notional=1,000,000`; QFF multiplier 100; preserved 5 TWD fixture fee |
| `configs/live.example.toml` | Global live symbols/data/strategy/fees | One `qff_tsm` pair; explicit `notional=1,000,000`; QFF multiplier 100; preserved live QFF fee 88 TWD |
| `configs/replay.example.toml` | Global replay paths/strategy/fees | One `qff_tsm` pair; explicit `notional=1,000,000`; QFF multiplier 100; preserved 5 TWD replay fee |
| `configs/replay.fixture.toml` | Global frozen fixture paths/strategy/fees | One `qff_tsm` pair; explicit `notional=1,000,000`; QFF multiplier 100; preserved 5 TWD golden fee |

`sizing.mode` defaults to `fixed_lots` and `lots` defaults to `1`. Selecting
`notional` without `leg_notional_twd` raises a clear configuration error. CLI
`--pair` is resolved dynamically against configured IDs; it no longer hardcodes an
instrument pair in the parser.

## 6. Frozen fixture sizing declaration

`configs/replay.fixture.toml` explicitly contains:

```toml
[pairs.sizing]
mode = 'notional'
leg_notional_twd = 1000000.0

[pairs.fees]
us_leg_fee_bps = 5.0
tw_leg_fee_per_contract_twd = 5.0
tw_leg_tax_rate = 0.00002
```

The deliberately unrealistic 5 TWD QFF fee was not changed.

## 7. Contract multiplier test

Contract multiplier now comes directly from the selected pair's
`tw_leg.contract_multiplier`; ADR ratio likewise comes from
`us_leg.adr_share_ratio`. `FeeConfig` contains fee rates only. The sizing, tax,
margin denominator, fill reconstruction, and live price policy all receive the
selected pair values explicitly.

Dedicated test output:

```text
tests/unit/test_calendar_sizing.py::test_pair_contract_multiplier_sizes_qff_and_ccf_correctly[QFF-100.0-100] PASSED [ 50%]
tests/unit/test_calendar_sizing.py::test_pair_contract_multiplier_sizes_qff_and_ccf_correctly[CCF-2000.0-5] PASSED [100%]

====================== 2 passed, 16 deselected in 0.09s =======================
```

At a 100 TWD futures price and 1,000,000 TWD requested notional, QFF's multiplier
100 sizes to 100 contracts and CCF's multiplier 2,000 sizes to 5 contracts. Both
produce exactly 1,000,000 TWD actual leg notional. CCF exists only as test data;
no CCF/UMC runtime pair was configured.

The TAIFEX tax formula remains:

```python
round_half_up_nonnegative(price * contract_multiplier * tax_rate)
```

No borrow cost, per-share commission, or Phase 3 fee model was added.

## 8. Handoff questions

`docs/HANDOFF_QUESTIONS.md` has no open questions at Checkpoint 2. No Phase 1
ambiguity required an owner decision, and no new out-of-scope bug was recorded.

Phase 2 has not started.
