---
name: chatgpt-oracle-runtime
description: Execute one user-authorized Oracle temporary-chat mission with explicit model and effort, durable capture, and exact-run recovery. May be selected implicitly by the managed natural-routing policy for long, repository-wide, or multi-stage work.
---

# Oracle execution

Follow the installed `docs/AUTOMATION_POLICY.md`. Use one mission-based flow;
planning, research, review, and editing are prompt content, not modes. Preserve
the configured native commander and user-approved delegation.

When the installed global AGENTS policy enables Natural Oracle routing, this
skill may be selected without the user spelling out "use Oracle" on every
request. Keep short or mechanical work in native Codex; use this route for the
long or broad cases defined by that policy. Native Codex remains the owning
commander and performs the final local verification.

## Execute

Write the requested objective and scope in a UTF-8 mission inside the approved
project root. Preview without starting a browser or submitting a prompt:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" execute --project-root C:\project --mission-path C:\project\mission.md --model gpt-5.6-sol --effort extended --dry-run
```

For authorized execution remove `--dry-run`. The ordinary Windows Plus default
is **GPT-5.6 Sol → High (extended)**. Oracle 0.20.0 must verify the selected
GPT-5.6 Sol model and visible High effort before submission. Do not infer Pro
or Extra High from a slider maximum and never silently downgrade. Alternative
models/efforts are used only when the user explicitly requests them and the
current account exposes them.

Use the configured DevSpace app mention (default `@codex`) and exact project
root.
Do not change authentication, approved roots, account personalization, app
registration, or permissions. The runner enables and confirms personalization
for the owned temporary chat before submission without changing account settings.

## Capture and recover

Save the complete answer durably before closing the exact owned tab.
Captured output is evidence of capture, not proof that every mission action
succeeded. Report the actual result and relevant project-test evidence.
No mandatory `TASK_OUTCOME` marker, audit nonce, three receipts, or tool-call
sequence is required.

On timeout or connection failure retain the same run and tab:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" reconnect --run-dir C:\exact\run --dry-run
```

Remove `--dry-run` only to observe that same run. Reconnection is prompt-free;
never automatically replay a prompt or adopt another task's tab. If the tab
was lost, report that fact and obtain an explicit recovery decision.

New work has no archive/restore phase. Historical executors and schemas remain
exact-recovery-only; preserve their original records and authority rather than
rewriting old state or selecting a retired mode for a new submission.
