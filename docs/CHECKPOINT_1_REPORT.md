# Checkpoint 1 Report — BLOCKED (not a passing checkpoint)

Phase 0 is not complete. Work stopped under §0.1 because the reusable subprocess
transport and consolidated CLI routing contract are not fully defined. Phase 1 has
not started. The completed, independently verifiable work and all blocking decisions
are recorded below.

## 1. Golden baseline

`tests/integration/test_replay_golden.py` is green. The post-change replay processed
29,909 rows and the full summary still matches
`tests/fixtures/replay/golden_summary.json` (integer/string/null fields exactly;
float fields at relative tolerance `1e-9`).

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

## 2. Test result

```text
........................................................................ [ 18%]
........................................................................ [ 37%]
......ssssssss.......................................................... [ 56%]
........................................................................ [ 75%]
........................................................................ [ 93%]
........................                                                 [100%]
376 passed, 8 skipped in 129.23s (0:02:09)
```

Reconciliation against the original `372 passed / 8 skipped`:

| Change | Pass-count effect |
|---|---:|
| Task 2.3 replay golden test | +1 |
| ExecutionAdapter protocol/preflight tests | +3 |
| Parameterized rollover scenarios | 0 (two old cases remain two collected cases) |
| Live-gated smoke tests | 0; all 8 remain skipped |
| Final | 376 passed / 8 skipped |

## 3. CLI mapping — proposed only, blocked by Q4/Q5

No parser or dispatcher changes were made. The table is exhaustive over the current
14 commands and every current flag, but selector syntax marked `PROPOSED` is not an
implemented interface and must be confirmed first.

| Old command and flags | Proposed target | Status |
|---|---|---|
| `replay --config --max-bars --resume --reset-store` | `replay` with the same flags plus `--pair qff_tsm` | Unambiguous; not implemented because 4.3 is one blocked task |
| `summary --config --execution` | `summary` with the same flags plus `--pair qff_tsm` | Unambiguous; not implemented |
| `doctor --config --mode replay\|live\|order` | PROPOSED `status --doctor --doctor-mode replay\|live\|order --config` | Selector/default undefined |
| `live-dry-run --config --resume --reset-store --max-iterations --skip-warmup --ui --quiet-ui --no-color` | PROPOSED `live --mode dry-run` with every remaining flag unchanged plus `--pair qff_tsm` | Whether `--mode` is required is undefined |
| `live-status --config` | PROPOSED default `status --config --pair qff_tsm` | Default status view is undefined |
| `reconcile-brokers --config --readonly` | PROPOSED `status --reconcile --config --readonly --pair qff_tsm` | Status selector contract undefined |
| `clear-pause --config --readonly` | PROPOSED `recover --clear-pause --config --readonly --pair qff_tsm` | Recover selector/default undefined |
| `recover-manual-flat --config --readonly --apply --reason` | PROPOSED `recover --manual-flat` with every remaining flag unchanged plus `--pair qff_tsm` | Recover selector/default undefined |
| `warmup-live --config --reset-store` | `warmup` with the same flags plus `--pair qff_tsm` | Unambiguous; not implemented |
| `margin-check --config` | PROPOSED `status --margin --config --pair qff_tsm` | Status selector contract undefined |
| `live-execute --config --resume --reset-store --max-iterations --skip-warmup --ui --quiet-ui --no-color` | PROPOSED `live --mode execute` with every remaining flag unchanged plus `--pair qff_tsm` | Must never be a default; exact mode contract undefined |
| `exec-smoke --config --venue --symbol --lot --quantity --confirm-symbol --raw-json` | PROPOSED `admin --action exec-smoke` with every remaining flag unchanged | Admin selector syntax undefined; all env gates/confirm guard must remain in the existing handler |
| `manual-close --config --venue --symbol --side --lot --quantity --confirm-symbol --raw-json` | PROPOSED `admin --action manual-close` with every remaining flag unchanged | Admin selector syntax undefined; all env gates/confirm guard must remain in the existing handler |
| `broker-status --config --funds --orders --raw-json` | PROPOSED `status --broker` with every remaining flag unchanged | Status selector contract undefined |

The heading “14 subcommands → 6” conflicts with the target table, which contains
seven top-level names when gated `admin` is counted. No interpretation was chosen.

## 4. Removed/merged tests

No behavior test was deleted. These named tests were merged into one parameterized
body; pytest still collects both cases.

| Previous test | Replacement | Why coverage is unchanged |
|---|---|---|
| `test_expiry_buffer_selects_front_contract_when_buffer_is_satisfied` | `test_expiry_buffer_selects_contract_for_business_day_distance[2026-07-08...-QFFG6-5]` | Preserves the front-contract symbol and business-day assertions |
| `test_expiry_buffer_switches_to_next_contract_when_front_has_four_days_left` | `test_expiry_buffer_selects_contract_for_business_day_distance[2026-07-09...-QFFH6-29]` | Preserves rollover selection and adds the selected contract's business-day assertion |

Duplicated fixture/config builders consolidated without deleting tests:

| Removed local helper | Shared replacement |
|---|---|
| `test_margin_check.write_config` | `tests/fakes.py::write_test_config` with a `margin_enabled=True` partial |
| `test_reconciliation_store_cli.write_config` | Shared helper with broker-reconciliation section enabled |
| `test_recovery_cli.write_config` | Shared helper with safety and broker-reconciliation sections enabled |

## 5. ExecutionAdapter protocol

The pre-existing one-method protocol in `lux_trader/execution/outcome.py` was expanded
in place. This keeps it beside `ExecutionOutcome`/`PairExecutionPlan`, where current
coordinators already import it, instead of coupling execution to the reconciliation
package. `PlanExecutor` retains the intentionally smaller seam used by simulated
execution.

```python
@dataclass(frozen=True)
class ExecutionPreflight:
    open_orders: tuple[dict[str, Any], ...]
    position_quantity: float

class PlanExecutor(Protocol):
    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome: ...

@runtime_checkable
class ExecutionAdapter(Protocol):
    def execute(self, plan: PairExecutionPlan) -> ExecutionOutcome: ...
    def fetch_open_orders(self) -> tuple[dict[str, Any], ...]: ...
    def fetch_position_quantity(self) -> float: ...
    def preflight(self) -> ExecutionPreflight: ...
    def close(self) -> None: ...
```

Fubon and Binance preflight records had exactly the same fields, so they now alias
the single `ExecutionPreflight` dataclass without information loss. Fubon-only
`fetch_order_records` and `session_health` remain off the shared protocol and are
represented by `OrderRecordsProvider` and `SessionHealthProvider`; the existing
runtime health call remains capability-checked with `getattr`.

The transport extraction is not implemented pending Q3. In particular, the Fubon
execution worker remains symbol-bound, as required for Phase 0.

## 6. Changes outside §4

| Change | Justification |
|---|---|
| Golden JSON and integration test | Explicit Task 2.3 prerequisite |
| `PlanExecutor` narrow protocol | Prevents the broader venue protocol from falsely requiring query/preflight methods on the simulated execution adapter |
| This report and `HANDOFF_QUESTIONS.md` | Required by §0.1 and §5 |

No production behavior, order construction, order timing, fill confirmation, or env
gate was changed.

## 7. Open questions / blockers

See `docs/HANDOFF_QUESTIONS.md` for the full context, options, and recommendations:

1. disposition of `issue/M6_FUBON_SYMBOL_FORMAT_ISSUE.md`;
2. deletion of three protected `.tmp_pytest*` directories;
3. readonly subprocess 20-second default versus the spec's 15-second query timeout;
4. consolidated CLI selector/default contract;
5. legacy CLI alias/deprecation policy.

## 8. LOC before/after

Counts are physical Python lines from tracked `master` versus the current worktree,
grouped by the first path component under `lux_trader/`. The tests row includes all
Python files under `tests/`; JSON fixtures are excluded.

| Top-level module | Before | After | Delta |
|---|---:|---:|---:|
| `__init__.py` | 3 | 3 | 0 |
| `__main__.py` | 5 | 5 | 0 |
| `brokers/` | 46 | 46 | 0 |
| `cli/` | 2,217 | 2,217 | 0 |
| `config.py` | 470 | 470 | 0 |
| `core/` | 1,815 | 1,815 | 0 |
| `dashboard_ui.py` | 458 | 458 | 0 |
| `execution/` | 2,119 | 2,163 | +44 |
| `integrations/` | 5,406 | 5,407 | +1 |
| `margin/` | 874 | 874 | 0 |
| `market_data/` | 1,256 | 1,256 | 0 |
| `ntfy.py` | 759 | 759 | 0 |
| `persistence/` | 802 | 802 | 0 |
| `reconciliation/` | 578 | 578 | 0 |
| `runner.py` | 169 | 169 | 0 |
| `runtime/` | 3,937 | 3,937 | 0 |
| `store.py` | 1,062 | 1,062 | 0 |
| `terminal_ui.py` | 585 | 585 | 0 |
| `trade_pnl.py` | 67 | 67 | 0 |
| **`lux_trader/` total** | **22,628** | **22,673** | **+45** |
| **`tests/`** | **14,595** | **14,682** | **+87** |

## 9. Plan/spec discrepancies observed

- `MULTIPAIR_PLAN.md` describes a broader venue interface (`quote`, historical data,
  place/cancel, positions, account), while §4.2 defines the current five-method
  execution adapter. The implementation spec wins; only the five-method protocol was
  formalized.
- The implementation spec names branch `feature/multipair-phase-0-1`; the owner's
  direct instruction for this run names `feature/multipair-phase`. The direct
  instruction was followed.
- The plan says “about 6” CLI commands and the implementation-spec heading says 6,
  but its target table lists 7 including `admin`. This remains unresolved with the
  rest of Q4/Q5.
