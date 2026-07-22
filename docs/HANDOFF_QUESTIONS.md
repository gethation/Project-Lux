# Handoff Questions

## Q1 — Decide the disposition of the resolved Fubon symbol issue
- **Blocking:** 4.1 (`issue/M6_FUBON_SYMBOL_FORMAT_ISSUE.md` only)
- **Context:** Phase 0 dead-code cleanup requires reviewer confirmation before archiving or deleting this issue file.
- **The ambiguity:** The implementation spec does not state whether the Fubon symbol-format issue has been resolved and the issue file itself is still tracked.
- **Options I see:** Keep it in place (no loss, but retains a possibly stale issue); archive it under the documentation tree (preserves history while marking it inactive); delete it (least clutter, but removes the tracked write-up).
- **My recommendation:** Keep it unchanged until the reviewer confirms its status; do not infer resolution from the surrounding Phase 0 work.

## Q2 — Remove protected stale pytest directories
- **Blocking:** 4.1 (`.tmp_pytest*` cleanup only)
- **Context:** All three exact workspace paths were verified before deletion, but the current Windows account cannot enumerate their contents or read their ACLs; native `Remove-Item` is denied before execution.
- **The ambiguity:** The spec requires deletion, but this session lacks filesystem authority to inspect or remove these directories and does not define an escalation procedure.
- **Options I see:** Have the owner remove the three directories from an elevated shell (completes the cleanup); grant the current account access and rerun cleanup (changes ACLs); leave the ignored directories in place (no repository impact, but task 4.1 remains operationally incomplete).
- **My recommendation:** Remove exactly `.tmp_pytest`, `.tmp_pytest_live_execute`, and `.tmp_pytest_live_execute_core` from an elevated PowerShell session; do not change broader workspace ACLs.

## Q3 — Resolve the readonly subprocess timeout contradiction
- **Blocking:** 4.2 reusable subprocess transport extraction only
- **Context:** `execution_process.py` defaults execution/query/terminate timeouts to 30/15/3 seconds, while `readonly_process.py` defaults its query timeout to 20 seconds.
- **The ambiguity:** Section 4.2 requires the extraction to remain functionally identical and also names the shared timeouts as 30/15/3 seconds. Changing readonly from 20 to 15 changes behavior; retaining 20 does not match the listed shared timeout.
- **Options I see:** Preserve the readonly 20-second default as an adapter-level override (behavior-preserving, but the extracted transport has more than the three listed defaults); change readonly to 15 seconds (matches the list, but changes a broker query timeout); leave both process implementations unextracted (no behavior change, but 4.2 remains incomplete).
- **My recommendation:** Preserve the readonly 20-second adapter override and extract only the transport mechanics; this honors the stronger no-live-behavior-change invariant, but requires reviewer confirmation before implementation.

## Q4 — Define the consolidated CLI routing contract
- **Blocking:** 4.3
- **Context:** The target table says which old commands are absorbed, but the parser must still choose one existing handler and provide every attribute that handler expects.
- **The ambiguity:** The spec does not define whether `live --mode` is required or defaults to dry-run; which mutually exclusive flags select the five `status` handlers; or whether `recover` and `admin` use action flags, an `--action` value, or nested subcommands. Defaults and mutual-exclusion rules are also unspecified.
- **Options I see:** Use required `--action`/`--view` values (explicit and easy to validate, but differs from the plan's “via flags” wording); use mutually exclusive boolean flags such as `status --reconcile` and `recover --manual-flat` (matches the plan, but needs defined defaults); use nested subcommands such as `status reconcile` and `admin manual-close` (clearest help output, but technically introduces another command layer).
- **My recommendation:** Require `live --mode dry-run|execute`; use mutually exclusive boolean action flags for `status` and `recover`, with read-only `live-status` as the only safe status default; use a required `admin --action exec-smoke|manual-close`. No action should default to a real-order handler.

## Q5 — Decide whether legacy CLI command names remain as aliases
- **Blocking:** 4.3
- **Context:** Consolidating 14 top-level commands into the target surface requires deciding what happens to scripts and operators still invoking the old names.
- **The ambiguity:** “14 subcommands → 6” suggests removing old names, while “every existing flag must remain reachable” could also be read as requiring compatibility aliases. The spec does not state a deprecation policy.
- **Options I see:** Remove all old names immediately (meets the command-count target, but breaks existing scripts); keep hidden aliases that route to the same handlers (preserves scripts, but the parser still accepts 14 legacy commands); keep aliases for one release with explicit warnings (safest rollout, but needs warning text and removal timing).
- **My recommendation:** Keep one-release compatibility aliases with a deterministic deprecation warning, then remove them after runbooks and operator scripts are migrated; confirm whether checkpoint command-counting permits hidden aliases.
