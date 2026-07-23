# Implementation Spec — Phase 0 (Slimming) and Phase 1 (Pair Generalization)

**Audience:** the coding agent implementing this work.
**Reviewer:** the planning agent, who verifies each checkpoint before you continue.
**Design rationale:** `docs/MULTIPAIR_PLAN.md`. Read it first. This document is the
*executable* form of that plan; where the two disagree, **this document wins** and you
must report the discrepancy.

---

## 0. Working agreement

### 0.1 The stop-and-ask rule — this is the most important rule in this document

**When you hit anything this spec does not explicitly define, STOP. Do not guess, do
not pick "the reasonable default", do not proceed and flag it later.**

Write the question into `docs/HANDOFF_QUESTIONS.md` using the format below, stop work
on that thread, and continue only with tasks that are unblocked. If nothing is
unblocked, stop entirely and hand back.

```markdown
## Q<n> — <one-line title>
- **Blocking:** <task id, e.g. 0.2.3>
- **Context:** <what you were doing, which files>
- **The ambiguity:** <precisely what is undefined>
- **Options I see:** <2-4 options with the tradeoff of each>
- **My recommendation:** <one, with reasoning — but do NOT implement it>
```

This project trades real money. A plausible-looking guess that silently changes
trading behavior is far more expensive than a blocked afternoon. Questions are
cheap and welcome; there is no penalty for asking.

**Block at the finest granularity that is actually blocked.** A question about one
part of a task does not block the parts that are already unambiguous. If your own
analysis marks some items "unambiguous" and others "undefined", implement the
unambiguous ones and block only the rest — then say exactly which sub-items remain
open. Ask precisely; do not stop broadly.

### 0.2 Scope discipline

- Implement **only** what is listed here. No opportunistic refactors, no drive-by
  fixes, no dependency upgrades, no reformatting of files you are not otherwise editing.
- If you find a bug that is out of scope, write it into `docs/HANDOFF_QUESTIONS.md`
  under a `## BUG` heading and leave the code alone.

### 0.3 Git

- Work on branch `feature/multipair-phase-0-1`, branched from `master`.
- Commit per task id (e.g. `phase 0.2: formalize VenueAdapter protocol`).
- Do not merge to `master`. Do not rebase or force-push. Do not amend commits that
  are already part of a submitted checkpoint.
- Do not commit: `.env`, `*.pfx`, any `data/` or `log/` content, SQLite stores.

### 0.4 Decisions already made — do not re-litigate

These were decided by the project owner. If you believe one is wrong, say so in
`HANDOFF_QUESTIONS.md`; do not implement an alternative.

| Decision | Value |
|---|---|
| Runtime topology | Single process, multiple pair contexts (Fubon allows one SDK session per account) |
| Leg naming | Structure is `tw_leg` / `us_leg`; **instrument identity (QFF/CCF/TSM/UMC) is data, not a field name** |
| Schema | Breaking change is allowed and expected. **Do not write a migration.** Old stores are archived, not upgraded |
| Position sizing | Explicit `sizing.mode`, default `fixed_lots`, default `lots = 1`. `notional` mode must be requested explicitly |
| FX source | Per pair. QFF/TSM keeps BitoPro USDT/TWD; CCF/UMC will use a real USD/TWD vendor (Phase 4, not your concern) |
| Old store | Archive, do not migrate |

---

## 1. Environment

Python runs through the Miniconda `Quant` environment. The shell is PowerShell on
Windows.

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m pytest -q
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader <command>
```

`.\scripts\lux.ps1 <command>` is a convenience wrapper for the second form.

**Never run any command that can place a real order.** Everything in Phase 0 and
Phase 1 is offline. Specifically: do not run `live-execute`, `exec-smoke`,
`manual-close`, or any test requiring `LUX_LIVE_MARKETDATA=1`,
`LUX_READONLY_BROKER=1`, or `*_ALLOW_LIVE_ORDER=1`. The 8 skipped tests in the
baseline are gated smoke tests and **must stay skipped**.

---

## 2. Baseline — establish this before changing anything

Run both of these on a clean checkout of `master` and confirm you reproduce them
exactly. If you do not, **stop and report** — your environment differs from the
reviewer's and nothing downstream will be trustworthy.

### 2.1 Test suite

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m pytest -q
```

Expected: **372 passed, 8 skipped** (~120s).

### 2.2 Replay golden baseline

```powershell
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader replay  --config configs/replay.fixture.toml --reset-store
& 'D:\Users\miniconda3\condabin\conda.bat' run -n Quant python -m lux_trader summary --config configs/replay.fixture.toml
```

Expected values — these are the **hard acceptance gate for Phase 1**:

| Field | Value |
|---|---|
| `rows` | `29909` |
| `trade_count` | `66` |
| `win_rate` | `0.803030303030303` |
| `total_pnl_twd` | `261507.82918245532` |
| `net_pnl_twd` | `261507.82918245535` |
| `gross_pnl_twd` | `329825.3260614279` |
| `total_fee_twd` | `68317.49687897251` |
| `total_tsm_fee_twd` | `62990.49687897251` |
| `total_qff_fee_twd` | `2720.0` |
| `total_qff_tax_twd` | `2607.0` |
| `max_drawdown_twd` | `-37238.95947526302` |
| `exposure_minutes` | `29487` |
| `final_equity_twd` | `2261507.8291824553` |
| `start` | `2026-05-08 17:25:00+08:00` |
| `end` | `2026-06-22 13:44:00+08:00` |

**Task 2.3 — capture the golden file.** Before any edit, save the full summary JSON
to `tests/fixtures/replay/golden_summary.json` and commit it. Add a test
`tests/integration/test_replay_golden.py` that runs the fixture replay and asserts
the summary matches this file: integer and string fields exactly, float fields within
relative tolerance `1e-9`. This test is your regression tripwire for everything that
follows — write it first, before you change any production code.

---

## 3. Invariants — violating any of these fails the checkpoint

1. **The replay golden baseline must not change.** Not by one ULP beyond the `1e-9`
   tolerance. Phase 0 and Phase 1 are behavior-preserving refactors. If you find
   yourself reasoning about *why* a P&L change is acceptable, you have made a
   mistake — stop and ask.
2. **`configs/replay.fixture.toml` semantics must not change.** It is a frozen
   regression baseline. In Phase 1 it must explicitly declare `mode = "notional"`
   and `leg_notional_twd = 1000000.0`, and keep `qff_fee_per_contract_twd = 5.0`
   (this value is deliberately unrealistic; it is a baseline, not a live config).
3. **No live-order code path may change behavior.** You may move it, rename its
   symbols, and change its call signature. You may not change what it sends, when it
   sends it, or how it confirms a fill.
4. **The 8 gated smoke tests stay skipped and unmodified in behavior.**
5. **No new third-party dependencies** without asking.

---

## 4. Phase 0 — Slimming

Goal: reduce the surface that Phase 1 has to generalize. Everything here is
behavior-preserving.

### 4.0 Baseline artifacts
Complete task 2.3 first. Do not start 4.1 until the golden test is committed and green.

### 4.1 Dead code and leftovers

| Item | Action |
|---|---|
| `.tmp_pytest/`, `.tmp_pytest_live_execute/`, `.tmp_pytest_live_execute_core/` | Delete. Already gitignored; they are stale test debris |
| `config.live.smoke.local.toml` (repo root) | Move to `configs/`. Update any reference |
| `issue/M6_FUBON_SYMBOL_FORMAT_ISSUE.md` | **RESOLVED (Q1)** — the issue is fixed. Delete the file; git history retains it. Remove the now-empty `issue/` directory |
| `docs/M6_RUNBOOK.md` | Already resolved — deleted in commit `8171132`. No action needed |
| `.tmp_pytest*` (three dirs) | **BLOCKED (Q2) — not your task.** Confirmed a genuine Windows ACL restriction; the reviewer reproduced `UnauthorizedAccessException` independently. The owner will remove them from an elevated shell. Do not retry, do not change ACLs, do not treat this as outstanding work |
| `configs/live.example.toml` → `qff_fee_per_contract_twd` | Change `5.0` → `88.0`. The live configs already use 88.0; the example under-states the real fee by 17.6x. **Do not change the `replay.*.toml` copies** — see invariant 2 |

**Do not remove BitoPro.** QFF/TSM continues to use it as its FX source.

### 4.2 Formalize the venue adapter protocol

This is **extraction, not design**. The two execution adapters already expose an
almost identical implicit interface. Your job is to make it explicit.

Current surface:

| Method | Fubon | Binance |
|---|---|---|
| `execute(plan: PairExecutionPlan) -> ExecutionOutcome` | ✓ | ✓ |
| `fetch_open_orders() -> tuple[dict, ...]` | ✓ | ✓ |
| `fetch_position_quantity() -> float` | ✓ | ✓ |
| `preflight() -> <Venue>ExecutionPreflight` | ✓ | ✓ |
| `close() -> None` | ✓ | ✓ |
| `fetch_order_records() -> tuple[dict, ...]` | ✓ | — |
| `session_health() -> dict` | ✓ | — |

`ReadOnlyBroker` in `lux_trader/reconciliation/brokers.py:15` is already a `Protocol`
(`broker`, `fetch_snapshot()`, `close()`). Follow that style.

Tasks:

1. Define `ExecutionAdapter` as a `typing.Protocol` covering the five shared methods.
   Put it next to the existing `ReadOnlyBroker` protocol or in a sibling module —
   your choice, but state which and why in the checkpoint report.
2. Fubon-only methods (`fetch_order_records`, `session_health`) stay off the shared
   protocol. Access them through a narrower optional protocol or an explicit
   `hasattr`/`getattr` check at the call site, consistent with how
   `runtime/live/contracts.py` already handles optional provider capabilities.
3. Unify the preflight return type, **or** state in the checkpoint report why the two
   cannot be unified. Do not force a unification that loses information.
4. Extract the subprocess-isolation transport currently hardcoded in
   `integrations/fubon/execution_process.py` and `readonly_process.py` into a reusable
   module. It must remain functionally identical: same error wrapping, same close
   semantics, and **every existing timeout preserved exactly as-is**.

   **Q3 resolved — this spec was wrong.** An earlier revision listed 30/15/3 as "the"
   shared timeouts; those are `execution_process.py`'s values. `readonly_process.py`
   uses a **20-second** query timeout. Behavior preservation outranks uniformity:
   keep 20s for readonly as an adapter-level override. The extracted transport takes
   timeouts as parameters and hardcodes none. Do not "harmonize" the two.

**Constraint:** do NOT make the Fubon execution worker symbol-agnostic in this phase.
That is Phase 2 work and it changes live behavior. Note it in the report and move on.

**Why this matters:** Phase 3 adds IBKR. If the protocol is right, IBKR implements one
interface once. If it is wrong, IBKR gets bolted on as a third special case.

### 4.3 CLI consolidation: 14 top-level commands → 7

> **Correction:** an earlier revision said "→ 6" while the table listed seven names.
> **Seven is correct** — the six operational commands plus the gated `admin`.

Current: `replay`, `summary`, `doctor`, `live-dry-run`, `live-status`,
`reconcile-brokers`, `clear-pause`, `recover-manual-flat`, `warmup-live`,
`margin-check`, `live-execute`, `exec-smoke`, `manual-close`, `broker-status`.

Target:

| Command | Absorbs |
|---|---|
| `replay` | `replay` |
| `summary` | `summary` |
| `live` | `live-dry-run`, `live-execute` (via `--mode dry-run\|execute`) |
| `status` | `live-status`, `broker-status`, `doctor`, `reconcile-brokers`, `margin-check` (via flags) |
| `recover` | `clear-pause`, `recover-manual-flat` (via flags) |
| `warmup` | `warmup-live` |
| `admin` | `exec-smoke`, `manual-close` — keep every existing env gate and confirm-symbol guard **exactly** as-is |

#### Routing contract (Q4 resolved) — use nested subcommands

Selectors are **nested subcommands**, not flags and not `--action` values:

```text
lux status live | status broker | status reconcile | status margin | status doctor
lux recover clear-pause | recover manual-flat
lux admin exec-smoke | admin manual-close
lux live --mode dry-run | live --mode execute
```

- `--mode` on `live` is **required**. There is no default. An operator must never
  reach a real-order path by omitting an argument.
- `status`, `recover`, and `admin` require an explicit subcommand — no default action.
  argparse enforces mutual exclusion naturally, which is precisely why this form was
  chosen over flags.
- `status doctor` keeps `--mode replay|live|order` as its own flag; it is unrelated to
  `live --mode`.
- All other flags carry over to their new home unchanged.

#### Legacy names (Q5 resolved)

**Remove all 14 old command names. Do not add aliases, hidden or otherwise.** Aliases
would leave the parser accepting 14 commands and defeat the consolidation. Update
`docs/LIVE_START_COMMANDS.md` to the new surface in the same commit.

Requirements:

- Every existing flag must remain reachable. Produce a **complete old→new mapping
  table** in the checkpoint report; the reviewer will check it line by line.
- `--mode execute` must retain every safety gate that `live-execute` has today:
  `safety.allow_live_order`, `[live_execution] enabled`, the three `*_ALLOW_LIVE_ORDER`
  env gates, and matched read-only reconciliation. **Making a real order easier to
  trigger by accident is the single worst outcome of this task.**
- Add `--pair <id>` to every command that operates on strategy state. In Phase 0 it
  accepts only the single implicit pair and may be a no-op placeholder; it becomes
  functional in Phase 1.
- `commands_execution.py` is 973 lines and `commands_live.py` is 592. Splitting them
  is allowed; the split is your call.

### 4.4 Test consolidation

39 test files, 14,595 lines. Reduce duplication **without reducing coverage**.

- Merge duplicated fixtures into `tests/conftest.py` / `tests/fakes.py`.
- Parameterize near-identical test bodies.
- **Every deleted or merged test must appear in a table in the checkpoint report**
  with the reason and the test that now covers that behavior. The reviewer will spot
  check these. Deleting a test because it is inconvenient is a checkpoint failure.
- Net test count may drop. Coverage of live-order gating, fill confirmation,
  reconciliation, and sizing may not.

---

## 5. CHECKPOINT 1 — stop here

Do not begin Phase 1. Produce `docs/CHECKPOINT_1_REPORT.md` containing:

1. **Golden baseline:** confirmation that `test_replay_golden.py` is green, plus the
   full summary JSON output pasted verbatim.
2. **Test result:** `pytest -q` output. State the new pass/skip counts and reconcile
   them against 372/8.
3. **CLI mapping table:** every old command+flag → new command+flag. Exhaustive.
4. **Removed/merged tests table:** each one, with justification and replacement.
5. **VenueAdapter protocol:** the final protocol definition, where you put it, and
   your answer on preflight unification.
6. **Anything you changed that is not listed in §4**, with justification.
7. **`docs/HANDOFF_QUESTIONS.md`** — all open questions.
8. **LOC before/after** per top-level module.

Then stop and hand back. The reviewer will re-run the baseline commands independently.

---

## 6. Phase 1 — Pair generalization

Goal: one code path serves N pairs. Instrument identity becomes data.

### 6.1 The naming rule

| Concept | Becomes | Example |
|---|---|---|
| The TAIFEX futures leg (QFF, CCF) | `tw_leg` | `MarketBar.tw_leg_close_filled` |
| The USD-denominated leg (TSM perp, UMC ADR) | `us_leg` | `MarketBar.us_leg_twd_fair` |
| `Direction.SHORT_TSM_LONG_QFF` | `Direction.SHORT_US_LONG_TW` | rendered as `Short TSM / Long QFF` |
| `BrokerName.FUBON_QFF` / `BINANCE_TSM` | venue-based, pair-agnostic | see 6.1.1 |

**The displayed name must always be the real instrument.** CLI output, terminal UI,
dashboard, logs, ntfy messages, and summary reports must show `QFF`/`CCF`/`TSM`/`UMC`,
resolved from pair configuration. A user must never see `tw_leg` in an interface.

#### 6.1.1 Scope boundary

**Generalize** (target: zero case-insensitive `qff`/`tsm` occurrences):
`lux_trader/core/`, `execution/`, `market_data/`, `persistence/`, `reconciliation/`,
`runtime/`, `store.py`, `config.py`, `terminal_ui.py`, `dashboard_ui.py`, `trade_pnl.py`, `cli/`

Mechanical check:

```bash
grep -ric "qff\|tsm" lux_trader/core lux_trader/execution lux_trader/market_data \
  lux_trader/persistence lux_trader/reconciliation lux_trader/runtime
```

**Keep venue-specific naming:** `integrations/fubon/`, `integrations/binance/`,
`integrations/bitopro/`, `integrations/taifex/`. These are venue adapters and their
names are correct. But **their instrument symbols must become parameters**, not
hardcoded values. Two known instances:

- `integrations/taifex/downloader.py:178` hardcodes `商品代號 == "QFF"`. Parameterize
  the product code. This single change is what later lets the same downloader fetch
  CCF tick data.
- `market_data/session.py` `select_qff_front_month(...)` already takes
  `product="QFF"` as a parameter — rename the function, keep the parameter.

### 6.2 Data model

Rename in `core/models.py` and every consumer: `MarketBar`, `IndicatorSnapshot`,
`PositionSizing`, `Position`, `OrderRequest`, `Fill`, `StrategyRuntimeState`,
`Direction`, `BrokerName`.

`OrderRequest`/`Fill` carry `qff_symbol` / `qff_expiry` / `contract_policy_state`.
These describe *the TAIFEX futures contract*, so they become `tw_leg_symbol` /
`tw_leg_expiry` / `contract_policy_state`.

### 6.3 Schema

Breaking change. **No migration.**

1. Add a `pairs` table: `pair_id TEXT PRIMARY KEY`, `label TEXT`,
   `tw_leg_display TEXT`, `us_leg_display TEXT`, `tw_leg_venue TEXT`,
   `us_leg_venue TEXT`, plus whatever else the runtime needs to render identity.
2. Add `pair_id` to every table carrying strategy state or market data:
   `strategy_state`, `events`, `orders`, `fills`, `positions`, `bars`, `trades`,
   `market_ticks`, `warmup_bars`, `execution_plans`, `execution_legs`,
   `execution_checks`, `execution_simulations`, `execution_outcomes`,
   `pending_manual_closes`, `position_adjustments`, `margin_checks`.
   **ASK** before adding `pair_id` to the broker/reconciliation tables
   (`broker_reconciliation_runs`, `broker_snapshots`,
   `broker_reconciliation_issues`, `fubon_*`) — those are account-scoped, not
   pair-scoped, and the right answer is not obvious.
3. `strategy_state`: drop `CHECK (id = 1)`; primary key becomes `pair_id`.
4. Rename `qff_*` / `tsm_*` columns to `tw_leg_*` / `us_leg_*`.
5. Bump the schema version and make the store **refuse to open an old store with a
   clear error message** telling the operator to archive it. Do not silently
   recreate, and do not attempt an in-place upgrade.

### 6.4 Config

```toml
[[pairs]]
id = "qff_tsm"
label = "QFF/TSM"

  [pairs.tw_leg]
  display = "QFF"
  venue   = "fubon"
  product = "QFF"
  contract_multiplier = 100.0

  [pairs.us_leg]
  display = "TSM"
  venue   = "binance"
  symbol  = "TSM/USDT:USDT"
  adr_share_ratio = 5.0

  [pairs.sizing]
  mode = "fixed_lots"       # default
  lots = 1                  # default
  # mode = "notional" requires:
  # leg_notional_twd = 1000000.0

  [pairs.strategy]
  entry_z = 2.0
  exit_z = 1.0
  zscore_window = 500
```

Requirements:

- `sizing.mode` defaults to `fixed_lots` with `lots = 1`. `notional` mode requires
  `leg_notional_twd` and must error clearly if it is missing.
- `configs/replay.fixture.toml` **must explicitly set `mode = "notional"` and
  `leg_notional_twd = 1000000.0`** — otherwise the default flip silently changes the
  frozen baseline. This is the highest-risk single line in Phase 1.
- `contract_multiplier` is **per pair**. QFF = 100, CCF = 2000. A wrong value here is
  a 20x position error. Add a dedicated test asserting both values size correctly.
- Migrate all existing configs in `configs/` to the new shape.
- Do not implement CCF/UMC yet. Phase 1 ships **one** pair (`qff_tsm`) through the
  generalized machinery. Adding a second pair is Phase 2.

### 6.5 Session calendar

Extract a `SessionCalendar` protocol from `core/calendar.py`.

- `TaifexSessionCalendar` preserves current behavior exactly: day 08:45–13:45,
  night 17:25–05:00, weekend force-close, `WEEKEND_FORCE_EXIT_GRACE_MINUTES = 5`.
- **Verified fact:** CCF has the identical TAIFEX session clock to QFF, so this one
  implementation serves both. Do not build the UMC-intersection calendar in Phase 1 —
  just make the seam.

### 6.6 Fees

Per-pair `FeeConfig`. Keep the existing math exactly, including
`round_half_up_nonnegative(price × multiplier × tax_rate)` per contract, which matches
the official TAIFEX per-contract rounding rule.

Do **not** add borrow-cost or per-share commission modeling — that is Phase 3.

---

## 7. CHECKPOINT 2 — stop here

Produce `docs/CHECKPOINT_2_REPORT.md`:

1. **Replay golden: byte-identical.** Paste the full summary JSON. Any deviation
   beyond `1e-9` relative is a hard failure — report it as a failure rather than
   adjusting the expected values.
2. **`pytest -q` output**, with pass/skip counts reconciled against Checkpoint 1.
3. **Mechanical naming check:** output of the `grep -ric` command in §6.1.1. Any
   non-zero count must be justified line by line.
4. **Schema diff:** old vs new DDL, plus the old-store rejection error message.
5. **Config migration:** before/after for every file in `configs/`.
6. **The `replay.fixture.toml` sizing declaration**, shown explicitly.
7. **The multiplier test** (QFF=100 / CCF=2000) and its output.
8. **`docs/HANDOFF_QUESTIONS.md`** — all open questions.

Then stop. Do not start Phase 2.

---

## Appendix A — verified reference facts

Established by investigation; do not re-derive.

| Fact | Value |
|---|---|
| QFF | 小型台積電期貨, **100 shares/contract**, TAIFEX code `QF`+`F` |
| CCF | 聯電股票期貨 (UMC), **2,000 shares/contract**, TAIFEX code `CC`+`F`. No mini contract exists |
| TAIFEX sessions | Day 08:45–13:45, night 17:25–05:00 — **identical for QFF and CCF** |
| Futures transaction tax | 2/100,000 of contract value, rounded per contract — same for both |
| QFF commission | 88 TWD/contract (live configs). CCF's rate is still unconfirmed with Fubon |
| ADR share ratio | 5 for both TSM and UMC |
| IBKR + TWD | IBKR does **not** quote or trade USD/TWD. Do not plan around it |

## Appendix B — files most affected

| Module | LOC | Notes |
|---|---|---|
| `runtime/` | — | 711 `qff`/`tsm` occurrences, the heaviest |
| `core/` | — | 418 occurrences; the frozen math lives here |
| `market_data/` | — | 326 occurrences |
| `execution/` | 2,119 | 235 occurrences; `real_coordinator.py` is 803 |
| `integrations/fubon/` | 3,751 | `execution.py` is 1,229 |
| `cli/` | 2,217 | `commands_execution.py` is 973 |
| `persistence/schema.py` | 439 | 24 tables |

Total: `lux_trader/` 22,628 lines across 93 files; 2,345 `qff`/`tsm` occurrences in
55 of them.
