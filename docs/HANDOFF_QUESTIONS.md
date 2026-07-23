# Handoff Questions

## Resolved questions

### Q1 — Resolved Fubon symbol issue disposition
- **Status:** Resolved.
- **Decision:** Delete `issue/M6_FUBON_SYMBOL_FORMAT_ISSUE.md`; the issue is fixed and Git history retains the write-up.
- **Outcome:** The file and now-empty `issue/` directory were removed in `b7b0b8b`.

### Q2 — Protected stale pytest directories
- **Status:** Resolved as outside this task.
- **Decision:** Do not retry deletion and do not change ACLs. The three ignored `.tmp_pytest*` directories have a reviewer-confirmed Windows ACL restriction and are for the owner to remove from an elevated shell.
- **Outcome:** No repository work remains for Q2; the ignored directories do not affect the committed checkpoint.

### Q3 — Readonly subprocess timeout
- **Status:** Resolved.
- **Decision:** Preserve execution timeouts `30/15/3` seconds and the readonly query timeout of `20` seconds. The reusable transport owns no request-timeout default; each adapter passes its existing timeout explicitly.
- **Outcome:** `SubprocessTransport` was extracted in `49847e5` without harmonizing the two adapters' timeout policies.

### Q4 — Consolidated CLI routing contract
- **Status:** Resolved.
- **Decision:** Use required nested subcommands for `status`, `recover`, and `admin`; use a required `live --mode dry-run|execute` selector with no default.
- **Outcome:** `7cce96a` implements exactly seven top-level commands: `replay`, `summary`, `live`, `status`, `recover`, `warmup`, and `admin`.

### Q5 — Legacy CLI command names
- **Status:** Resolved.
- **Decision:** Remove all legacy names without aliases or a deprecation window.
- **Outcome:** The 12 absorbed top-level names are rejected; `replay` and `summary` remain under their existing names.

### Q6 — Fubon subprocess timing tests missed their 2-second deadline
- **Status:** Resolved in `c0a4acd`. Raising it was correct — declining to loosen an
  unrelated test to make your own work pass is exactly the right call.
- **Reviewer investigation:** Reproduced independently. Ran both tests 10 times with
  no code change at all: **4/5 failed without your changes, 2/5 with them**, so the
  IBKR work was not the cause. Then measured the underlying quantity directly —
  a cold Windows worker takes **1.73–1.92s** to spawn, import, and reply (10 samples),
  against a 2.0s deadline. The pair was running at 92–96% of its budget on every run
  and had always been fragile; IB Gateway competing for the machine merely made the
  failures frequent.
- **Decision:** Both tests now use `REBUILD_INIT_TIMEOUT_SECONDS = 5.0` (~2.6x
  headroom, empirically 5/5 stable). Assertions are unchanged, and production is
  untouched — the live provider uses `DEFAULT_INIT_TIMEOUT_SECONDS` (30.0s), so the
  2.0s only ever existed inside these two tests. The third test in the file keeps its
  1.0s because both of its workers hang by design and never need to answer.

## Open questions

None.

## BUG

None outstanding. The Windows-spawn timing fragility recorded here was diagnosed and
fixed in `c0a4acd`.
