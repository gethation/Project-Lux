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

## Open questions

None at Checkpoint 1.

## BUG

None found during the documentation-only checkpoint review.
