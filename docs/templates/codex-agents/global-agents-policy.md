<!-- BEGIN CODEX WEB GPT SUBAGENT POLICY -->
## Shared automation policy

- Follow the installed app's `docs/AUTOMATION_POLICY.md`; projects retain their
  own build, test, domain, and safety requirements.
- Preserve the configured commander, reasoning, and user-approved worker
  routing. Installation must not silently choose another main model.
- Use bounded workers with non-overlapping write scopes when useful. The
  commander integrates results and remains responsible for the task.
- Use one mission flow, explicit model and effort selection, temporary
  chats, durable result capture before closing the owned tab, and same-run
  recovery without automatic resubmission.
- Do not add execution modes, mandatory audit receipts/tool order, per-project
  qualification chains, or archive/restore stages for new work.
- Preserve authentication, approved roots, original profiles, and historical
  evidence. Never adopt, stop, or close another task's run or tab.
- Never restore removed CGW routing. Account and permission changes remain
  under user control.

## Natural Oracle routing

- Treat the user's approval of this managed policy as standing authority to
  choose the ordinary Oracle route when the task clearly benefits from it.
  Do not ask for per-run Oracle opt-in when the request is already authorized.
- Keep short, mechanical, single-file, or quick diagnostic work in native
  Codex. Prefer Oracle for repository-wide or multi-module analysis, long
  debugging, many-file/log/web investigations, multi-step implementation plus
  tests/E2E, work likely to exceed roughly 15 minutes, and requests framed as
  "finish it end-to-end", "analyze the whole thing", or "find the root cause".
- The ordinary automatic route uses the registered `@codex` DevSpace app with
  the exact approved project root and **GPT-5.6 Sol → High** (`extended`). Do
  not automatically select Latest, Pro, Extra High, Web Multi, or another
  premium/compatibility route; those require an explicit user request.
- Before creating a new Oracle run for the same task/root, inspect persisted
  ownership and lifecycle state. If a compatible run already exists, recover
  the exact existing run instead of resubmitting or creating a duplicate.
- Codex remains the owning commander: it scopes the mission, preserves user
  authority, validates the durable Oracle result, runs the relevant local
  checks, and integrates any changes. Never expand permissions or weaken
  project quality gates merely to make the web route succeed.
- If Oracle becomes unavailable before submission, continue locally only when
  native Codex can still satisfy the same objective without weakening scope or
  evidence. Otherwise report the concrete blocker; never silently downgrade
  model, effort, root, or transport.
<!-- END CODEX WEB GPT SUBAGENT POLICY -->
