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
