# Checkpoint 1 Report — Phase 0 complete

Phase 0 is complete on `feature/multipair-phase` through commit `b223138`. Phase 1
has not started.

## 1. Golden baseline

`tests/integration/test_replay_golden.py` is green in the full suite. The checkpoint
replay and summary were also rerun directly in the `Quant` environment:

```text
Replay complete: rows_processed=29909, start_row=0, end_row=29908, finalized=True
```

The result remains `rows=29909`, `trade_count=66`,
`net_pnl_twd=261507.82918245535`, and
`total_fee_twd=68317.49687897251`. Full `summary` JSON output, verbatim:

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

The golden test compares integer, string, and null fields exactly and floating-point
fields at relative tolerance `1e-9`.

## 2. Test result

Actual `pytest -q` output from the completed Phase 0 tree:

```text
........................................................................ [ 17%]
........................................................................ [ 34%]
......ssssssss.......................................................... [ 51%]
........................................................................ [ 68%]
........................................................................ [ 86%]
..........................................................               [100%]
410 passed, 8 skipped in 165.08s (0:02:45)
```

The eight live-gated smoke tests remain skipped. The count reconciles against both
the original baseline and the intermediate checkpoint as follows:

| Change | Collected-pass effect | Running result |
|---|---:|---:|
| Original baseline | — | 372 passed / 8 skipped |
| Golden replay regression | +1 | 373 / 8 |
| ExecutionAdapter/preflight protocol tests | +3 | 376 / 8 |
| Intermediate checkpoint | — | 376 passed / 8 skipped |
| Reusable subprocess transport timeout-policy test | +1 | 377 / 8 |
| CLI mapping cases, one per implemented route | +14 | 391 / 8 |
| Exact seven-command surface test | +1 | 392 / 8 |
| Required `live --mode` and nested-action cases | +4 | 396 / 8 |
| Rejection cases for the 12 retired top-level names | +12 | 408 / 8 |
| Explicit live dispatch cases (`dry-run`, `execute`) | +2 | 410 / 8 |
| Parameterized rollover merge | 0; two old tests became two collected cases | 410 / 8 |
| Final | +38 from original; +34 from intermediate | **410 passed / 8 skipped** |

## 3. Exhaustive CLI mapping

All 17 help surfaces (root, seven top-level commands, and all nested subcommands)
were run directly and exited successfully. The root surface is:

```text
usage: lux_trader [-h] {replay,summary,live,status,recover,warmup,admin} ...
```

The nested surfaces are `status {live,broker,doctor,reconcile,margin}`,
`recover {clear-pause,manual-flat}`, and `admin {exec-smoke,manual-close}`.
`live --mode {dry-run,execute}` is required and has no default. `status`, `recover`,
and `admin` also require an explicit nested subcommand. No legacy aliases are
registered.

Every old command and every old flag maps as follows. “Unchanged” means spelling,
type, choices, requiredness, and flag semantics were preserved at the new route.

| Old command / flag | Implemented new home | Mapping detail |
|---|---|---|
| `replay` | `replay` | Top-level name retained |
| `replay --config` | `replay --config` | Unchanged |
| `replay --max-bars` | `replay --max-bars` | Unchanged |
| `replay --resume` | `replay --resume` | Unchanged |
| `replay --reset-store` | `replay --reset-store` | Unchanged |
| `summary` | `summary` | Top-level name retained |
| `summary --config` | `summary --config` | Unchanged |
| `summary --execution` | `summary --execution` | Unchanged |
| `doctor` | `status doctor` | Absorbed as required nested action |
| `doctor --config` | `status doctor --config` | Unchanged |
| `doctor --mode replay\|live\|order` | `status doctor --mode replay\|live\|order` | Unchanged; default remains `replay`; unrelated to `live --mode` |
| `live-dry-run` | `live --mode dry-run` | Old command becomes the required mode selector |
| `live-dry-run --config` | `live --mode dry-run --config` | Unchanged |
| `live-dry-run --resume` | `live --mode dry-run --resume` | Unchanged |
| `live-dry-run --reset-store` | `live --mode dry-run --reset-store` | Unchanged |
| `live-dry-run --max-iterations` | `live --mode dry-run --max-iterations` | Unchanged |
| `live-dry-run --skip-warmup` | `live --mode dry-run --skip-warmup` | Unchanged |
| `live-dry-run --ui dashboard\|compact` | `live --mode dry-run --ui dashboard\|compact` | Unchanged; default remains `compact` |
| `live-dry-run --quiet-ui` | `live --mode dry-run --quiet-ui` | Unchanged |
| `live-dry-run --no-color` | `live --mode dry-run --no-color` | Unchanged |
| `live-status` | `status live` | Absorbed as required nested action |
| `live-status --config` | `status live --config` | Unchanged |
| `reconcile-brokers` | `status reconcile` | Absorbed as required nested action |
| `reconcile-brokers --config` | `status reconcile --config` | Unchanged |
| `reconcile-brokers --readonly` | `status reconcile --readonly` | Unchanged, including its live-readonly env requirement |
| `clear-pause` | `recover clear-pause` | Absorbed as required nested action |
| `clear-pause --config` | `recover clear-pause --config` | Unchanged |
| `clear-pause --readonly` | `recover clear-pause --readonly` | Unchanged, including its live-readonly env requirement |
| `recover-manual-flat` | `recover manual-flat` | Absorbed as required nested action |
| `recover-manual-flat --config` | `recover manual-flat --config` | Unchanged |
| `recover-manual-flat --readonly` | `recover manual-flat --readonly` | Unchanged, including its live-readonly env requirement |
| `recover-manual-flat --apply` | `recover manual-flat --apply` | Unchanged; default remains dry-run |
| `recover-manual-flat --reason` | `recover manual-flat --reason` | Unchanged; required by the handler when `--apply` is used |
| `warmup-live` | `warmup` | Renamed top-level command |
| `warmup-live --config` | `warmup --config` | Unchanged |
| `warmup-live --reset-store` | `warmup --reset-store` | Unchanged |
| `margin-check` | `status margin` | Absorbed as required nested action |
| `margin-check --config` | `status margin --config` | Unchanged |
| `live-execute` | `live --mode execute` | Old command becomes the required mode selector; no default reaches execute |
| `live-execute --config` | `live --mode execute --config` | Unchanged |
| `live-execute --resume` | `live --mode execute --resume` | Unchanged |
| `live-execute --reset-store` | `live --mode execute --reset-store` | Unchanged |
| `live-execute --max-iterations` | `live --mode execute --max-iterations` | Unchanged |
| `live-execute --skip-warmup` | `live --mode execute --skip-warmup` | Unchanged |
| `live-execute --ui dashboard\|compact` | `live --mode execute --ui dashboard\|compact` | Unchanged; default remains `compact` |
| `live-execute --quiet-ui` | `live --mode execute --quiet-ui` | Unchanged |
| `live-execute --no-color` | `live --mode execute --no-color` | Unchanged |
| `exec-smoke` | `admin exec-smoke` | Absorbed as required gated admin action |
| `exec-smoke --config` | `admin exec-smoke --config` | Unchanged |
| `exec-smoke --venue fubon\|binance` | `admin exec-smoke --venue fubon\|binance` | Unchanged and still required |
| `exec-smoke --symbol` | `admin exec-smoke --symbol` | Unchanged |
| `exec-smoke --lot` | `admin exec-smoke --lot` | Unchanged |
| `exec-smoke --quantity` | `admin exec-smoke --quantity` | Unchanged |
| `exec-smoke --confirm-symbol` | `admin exec-smoke --confirm-symbol` | Unchanged and still required |
| `exec-smoke --raw-json` | `admin exec-smoke --raw-json` | Unchanged |
| `manual-close` | `admin manual-close` | Absorbed as required gated admin action |
| `manual-close --config` | `admin manual-close --config` | Unchanged |
| `manual-close --venue fubon\|binance` | `admin manual-close --venue fubon\|binance` | Unchanged and still required |
| `manual-close --symbol` | `admin manual-close --symbol` | Unchanged and still required |
| `manual-close --side buy\|sell` | `admin manual-close --side buy\|sell` | Unchanged and still required |
| `manual-close --lot` | `admin manual-close --lot` | Unchanged |
| `manual-close --quantity` | `admin manual-close --quantity` | Unchanged |
| `manual-close --confirm-symbol` | `admin manual-close --confirm-symbol` | Unchanged and still required |
| `manual-close --raw-json` | `admin manual-close --raw-json` | Unchanged |
| `broker-status` | `status broker` | Absorbed as required nested action |
| `broker-status --config` | `status broker --config` | Unchanged |
| `broker-status --funds` | `status broker --funds` | Unchanged |
| `broker-status --orders SYMBOL` | `status broker --orders SYMBOL` | Unchanged |
| `broker-status --raw-json` | `status broker --raw-json` | Unchanged |

Phase 0 also adds `--pair qff_tsm` to strategy-state routes. It is optional at parse
time, defaults to the only accepted value `qff_tsm`, and is present on `replay`,
`summary`, both `live` modes, `status live`, `status reconcile`, `status margin`,
both `recover` actions, and `warmup`. It is intentionally absent from generic
`status doctor`, account-level `status broker`, and the single-venue `admin` tools.

The routing-only claim for the live-order invariant has especially strong evidence:
`lux_trader/cli/commands_execution.py`, `commands_live.py`, and
`commands_recovery.py` are byte-identical to `master`. Thus the order handlers,
safety gates, reconciliation checks, order construction, and fill confirmation were
not edited; only parser/dispatcher routing changed.

## 4. Removed and merged tests

No behavior test was dropped. Exactly two named tests were merged into one
parameterized body, and pytest still collects two cases:

| Previous test | Replacement collected case | Why coverage is unchanged |
|---|---|---|
| `test_expiry_buffer_selects_front_contract_when_buffer_is_satisfied` | `test_expiry_buffer_selects_contract_for_business_day_distance[2026-07-08T09:00:00+08:00-QFFG6-5]` | Preserves front-contract selection and the five-business-day assertion |
| `test_expiry_buffer_switches_to_next_contract_when_front_has_four_days_left` | `test_expiry_buffer_selects_contract_for_business_day_distance[2026-07-09T09:00:00+08:00-QFFH6-29]` | Preserves rollover-to-next-contract selection and asserts its business-day distance |

The following duplicated local config builders were merged into shared helpers.
These are fixture consolidations, not deleted test cases, and have no collection-count
effect:

| Removed local helper | Shared replacement | Preserved specialization |
|---|---|---|
| `test_margin_check.write_config` | `tests/fakes.py::write_test_config` via `partial` | `margin_enabled=True` default |
| `test_reconciliation_store_cli.write_config` | `tests/fakes.py::write_test_config` via `partial` | Broker reconciliation enabled |
| `test_recovery_cli.write_config` | `tests/fakes.py::write_test_config` via `partial` | Safety and broker reconciliation enabled |
| `test_live_execute_preflight.write_live_execute_config` | `tests/fakes.py::write_execution_test_config` via `partial` | Original config/store/cache names, reconciliation, and omitted Fubon env path |
| `test_binance_execution.write_config` | `tests/fakes.py::write_execution_test_config` | Existing safety/live-execution toggles and Binance defaults |
| `test_fubon_execution.write_config` | `tests/fakes.py::write_execution_test_config` | Existing safety/live-execution toggles and Fubon defaults |

Other touched tests retained their test functions and assertions while updating only
the invoked command route to the implemented nested CLI.

## 5. ExecutionAdapter protocol and subprocess transport

The final protocol lives in `lux_trader/execution/outcome.py`, beside
`ExecutionOutcome` and `PairExecutionPlan`, because execution coordinators already
depend on that module and this avoids coupling execution to reconciliation.
`PlanExecutor` remains the deliberately narrower seam used by simulated execution.

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

Fubon and Binance preflight records contained the same two fields, so
`FubonExecutionPreflight` and `BinanceExecutionPreflight` now alias the shared
`ExecutionPreflight` dataclass with no information loss. Fubon-only
`fetch_order_records()` and `session_health()` stay off the shared interface and are
described by the narrow runtime-checkable `OrderRecordsProvider` and
`SessionHealthProvider` protocols. Existing runtime access to session health remains
capability-checked with `getattr`.

`lux_trader/integrations/subprocess_transport.py` now owns the reusable mechanics
previously duplicated by `fubon/execution_process.py` and
`fubon/readonly_process.py`: spawn-context/Pipe/process lifecycle, serialized request
locking, send/poll/receive handling, worker-error wrapping, worker PID/liveness,
idempotent close, restart, terminate-then-kill, and connection cleanup. Worker
targets, payloads, labels, exception classes, and timeout policy remain supplied by
the adapters. The transport hardcodes no request timeout.

Q3 is preserved exactly:

| Adapter | Execution | Query | Terminate |
|---|---:|---:|---:|
| `FubonFutureExecutionProcess` | 30 s | 15 s | 3 s |
| `FubonReadOnlyBrokerProcess` | N/A | 20 s | 3 s |

The Fubon execution worker remains symbol-bound, as required; symbol generalization
was not started.

## 6. Changes outside §4

| Change | Justification |
|---|---|
| `tests/fixtures/replay/golden_summary.json` and `tests/integration/test_replay_golden.py` | Required by baseline Task 2.3 before Phase 0 work |
| `.gitignore`: `.codex_pytest_tmp/` and `.codex-logs/` | Prevent local pytest scratch and delegation logs from appearing as repository changes; no runtime effect |
| `docs/IMPLEMENTATION_SPEC_PHASE_0_1.md` Q1-Q5 resolution text | Records reviewer/owner decisions that unblocked the already-scoped Phase 0 tasks; no product behavior |
| This report and `docs/HANDOFF_QUESTIONS.md` | Required Checkpoint 1 deliverables |

`PlanExecutor` is not listed here: it is the narrow supporting protocol needed by the
§4.2 `ExecutionAdapter` extraction so simulated execution is not falsely required to
implement venue-query methods.

### Minor behavioral delta

The previous BLOCKED report's statement that “No production behavior ... was
changed” was slightly overstated. At the current location
`lux_trader/integrations/fubon/execution_process.py:169`, `preflight()` now validates
the subprocess result with `isinstance(result, ExecutionPreflight)` and raises
`FubonExecutionWorkerError` for an unexpected result type. Previously it returned
the worker result without this type check.

This is a safe defensive validation and stays, but it is a real minor behavioral
delta because malformed worker output now has a new raise path. It does not alter a
valid preflight result, an order payload, order timing, fill confirmation, or a live
safety gate.

## 7. Handoff questions

`docs/HANDOFF_QUESTIONS.md` marks Q1-Q5 resolved and records the implemented decision
for each. There are no new open questions and no newly discovered out-of-scope bugs.

## 8. LOC before/after

Counts are physical Python lines from the tracked `master` tree
(`8171132`) versus `b223138`, grouped by the first path component under
`lux_trader/`. The tests row includes all Python files under `tests/`; JSON fixtures
are excluded.

| Top-level module | Before | After | Delta |
|---|---:|---:|---:|
| `__init__.py` | 3 | 3 | 0 |
| `__main__.py` | 5 | 5 | 0 |
| `brokers/` | 46 | 46 | 0 |
| `cli/` | 2,217 | 2,295 | +78 |
| `config.py` | 470 | 470 | 0 |
| `core/` | 1,815 | 1,815 | 0 |
| `dashboard_ui.py` | 458 | 458 | 0 |
| `execution/` | 2,119 | 2,163 | +44 |
| `integrations/` | 5,406 | 5,434 | +28 |
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
| **`lux_trader/` total** | **22,628** | **22,778** | **+150** |
| **`tests/`** | **14,595** | **15,041** | **+446** |

Phase 0 therefore stops at Checkpoint 1 with the golden baseline intact; no Phase 1
schema, naming, configuration, calendar, or fee work has begun.
