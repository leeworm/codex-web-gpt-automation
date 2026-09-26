---
name: chatgpt-thinking-browser
description: Historical compatibility and exact-run recovery guidance for selector-era regular Oracle plus DevSpace runs. New ordinary work uses chatgpt-oracle-runtime.
---

# Historical regular Oracle + DevSpace compatibility

Do not select this skill for new ordinary work. Use `chatgpt-oracle-runtime`
under the shared Natural Oracle routing policy instead. This file remains only
to preserve the semantics and recovery contract of persisted selector-era
direct/plan/review/edit/orchestrator runs.

For an exact persisted compatibility run, preserve its recorded mission, model,
effort, root, and mode. Do not convert it to the new ordinary route or submit a
replacement prompt. Historical dispatch shape:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_dispatch.py" --mode <direct|plan|review|edit|orchestrator> --project-root C:\project --mission-path C:\project\mission.md --manifest-output C:\project\.ai-bridge\oracle.json --reasoning-level "Very High" --dry-run
```

The runtime sends the configured app mention (default `@codex`) plus the
absolute mission path. It never attaches files, opens ChatGPT settings,
inspects/selects/deletes an app, or falls back to
agbrowse, Playwright, in-app Browser, or Chrome.

`orchestrator` is a single web submission that carries the orchestrator
ownership contract: that one GPT session owns delegated exploration, code
authoring, tests, and internal parallel lanes, and its answer is the result.
It has no stages, no stage receipts, and no local gate. Do not confuse it with
comprehensive mode, which is a multi-stage workflow owned by
`chatgpt-pro-plan-handoff` and `bin/chatgpt_oracle_comprehensive.py`.
Comprehensive mode runs `orchestrator`-equivalent work as its implementation
stage, so it contains this mode rather than competing with it.

The `orchestrator` and comprehensive distinctions below describe only those
persisted historical workflows. They are not automatic route choices for new
requests.

CodexPro is frozen for new work. Never mention it in a new mission, probe its
endpoint, repair/register/delete its app, or use it as a DevSpace fallback.

For recovery, keep the model and effort recorded by the persisted run. Never
upgrade, downgrade, or reinterpret that historical selection from the current
Plus account defaults.

Every new run copies the manually signed-in Oracle profile into a throwaway
per-run profile and asks Oracle to hide its owned window. This isolates
different projects: one completed run cannot close another run's live Chrome.
Do not replace this with the shared manual-login profile.

Control state and final Oracle output are host-only below
`%USERPROFILE%\.codex\state\chatgpt-oracle`. Complete requires exit zero and
fresh nonempty host output. Recovery uses the stored slug:

```powershell
python "$env:USERPROFILE\.codex\bin\chatgpt_oracle_run.py" recover --run-dir C:\exact\host-run --action harvest
```

Recovery never restarts/resubmits and never downgrades durable COMPLETE. If the
persisted CDP endpoint died, Oracle may launch a bounded recovery browser from
the run's recorded profile seed and open only that slug's exact persisted
conversation URL for harvest. It must not use a prompt or create a replacement
conversation. Session authority is monotonic: a later `running` observation
cannot downgrade `terminal_observed`. That disagreement remains
attention-required with the same task-scoped project lock; a later exact terminal harvest
with fresh nonempty output settles it to COMPLETE.

For an already persisted agbrowse run only, use its exact legacy
`chatgpt_agbrowse_run.py --observe-run|--recover-run <run-dir>` command. Do not
create a new agbrowse run.
