# Orchestrator operating reference

Detail the root `SKILL.md` links to so the hub stays within its line budget. Authority order when anything here disagrees:
`MANIFESTO.md`, then the runtime (`office ... next:` lines, `docs/review-convergence.md`), then this page. If the runtime
refuses something this page allows, the runtime is right; fix this page.

## Install and update

- `office --version` not found: stop, ask the user to approve installing it, never install silently. On approval:
  `uv tool install "<skill directory>"` (`pipx install` if `uv` is missing), then `office install` (managed harness hooks and
  runtime registration, with a backup of each config) and `office doctor`.
- Release (the part before any `+`) differs from the directory's `src/office/_version.py`: tell the user and offer
  `uv tool install --force --reinstall "<skill directory>"`, then `office install`. Runs already started keep their pinned runtime.
- Same release: a wheel reports only its release, so later fixes are invisible to `--version`. Run `office doctor`; on
  `install: STALE`, tell the user and offer the same reinstall.
- At intake, run `office update --check`. If it reports an update, ask in the next native question round. A failed or
  offline check is informational.

## Dispatch recovery

A dispatch failure is a recovery checkpoint, not permission to abandon the run. Inspect the launch notice and
`office inspect task <T> --verbose`. Each dispatch's `launch` lines show the argv Office actually rendered for the Herdr
attempt and any headless fallback, with prompt transport, adapter hash and harness version. Prompts and credential values are
redacted there and in the private `launch.json`.
Before a visual capture starts its server, Office checks the local capture port (#507). It stops a holder only when it can
prove that holder is a leftover of an ended executor, headless or Herdr pane. The proof is that the holder runs in the agent
process group Office recorded for that dispatch at launch and started while the dispatch ran. If that group's leader still
runs, its start time must also equal the one recorded at launch. A pane dispatch with no recorded start is never matched.
Even then, Office does not stop it while another executor is live in that worktree. The stop is recorded as a
`visual.reclaimed` event. Any other holder becomes `CAPTURE_BLOCKED` naming the port, PID, command and whether an ended
dispatch may own it. A server that left the agent's group (for example via `setsid`) is always reported and never stopped.
When Office names a failed Herdr pane, read it yourself with `herdr pane read <pane>`
(or the saved `pane-tail.txt` once Office closed it) to find the startup dialog, dead harness, quota wall, or other blocker.
When a harness opens on an interactive startup screen (an update notice, a trust, login or what's-new screen),
Office keeps its Herdr pane and records a startup prompt instead of falling back to headless at once (#510). `office status`
and `office wait` (exit 5) print it as a `question:` line with the screen, its options, a snapshot path and the answer
command: `office answer <dispatch> --choice <n> --expect <fp>` or `--keys esc --expect <fp>` (`--keys` takes up to six of
esc, enter, up, down, left, right, tab, space, 1-9). Office checks that the pane still shows that exact prompt and that the
launch holding it is alive. It refuses a stale or concurrent answer. Once the harness is ready, Office delivers the brief once.
An answer that opens another screen is recorded as a new round with a new fingerprint. An unanswered prompt
(`OFFICE_STARTUP_PROMPT_WAIT`, default 900s), a closed pane or a dead launcher ends in an attributed headless fallback, or in
no relaunch when the dispatch was revoked meanwhile. `office inspect startup [dispatch]` lists every prompt and how it ended.
Office never answers one on its own: choosing Update now, trusting a folder, logging in or granting permissions is the
user's decision. Office answers a folder-trust dialog only for worktrees and dispatch directories it created. Never approve hook trust, other
directories, credentials, irreversible actions, or user authority on the user's behalf. Close a pane only by the id Office
names, never by matching screen text, and never your own (`$HERDR_PANE_ID`). Then follow the printed `next:`: re-prompt a live
pane, `office resume`, `office rerun <task> --resume|--fresh`, `office dispatch <task> --reroute`, or the documented
external/manual launch. Stop only when those are exhausted or a user decision is required.

## Route discovery and trial recovery

Read [route discovery](route-discovery.md) when inspecting a probe/trial decision, changing user routing preferences or disabling discovery. Invocation support, learned effectiveness and adapter trust are separate evidence; a probe pass grants no trust or final gate authority. Automatic trials are builder-only, bounded and reversible, with the recorded known-working fallback and normal independent review/verification. Discovery ships enabled for executor/worker in the config default, so only runs started after that release can discover. A pinned older run, a `v3.1` run and a 3.0 run stay off, and `routing.discovery.enabled: false` at user or repo tier disables it for future runs. In the first live trial a headless launch did not write `agent.env`, so the executor could not run the `. <agent.env> && office submit` line and the trial was recorded `abandoned`. Headless launches now write it, and a second live trial reached `trial-accepted` with no manual step (evidence doc). Do not report an `abandoned` trial as a clean trial.

For a trial launch failure before meaningful work, confirm the entire failed worker process tree has exited before releasing its lease/session and starting the recorded fallback. An unconfirmed exit blocks replacement; preserve work already started for explicit recovery. Manual pins skip discovery and never silently fall back. Use `office inspect route <task>` and `--json` for immutable attempt evidence, cap state, preference source and recovered launches. Config edits affect future runs; changing a live run's pinned routing remains an explicit opt-in.

## Waiting, questions, stalls

`dispatch --as` and `rerun --as` record orchestrator choices with ordinary routing safety gates. Add `--quote "<user's words>"` only for actual user authority. `amend route` can record an accepted task's future route without rewriting its submitted producer; its next legal reopen uses that route. Revoking new acceptance work does not mark the new criteria satisfied.

- `office wait` exits 0 (act), 3 (stall or an already-reported action with no work in flight), 5 (an agent asked a question), 124 (nothing new). On 3, follow `next:` before waiting again. An amendment awaiting an ended executor's acknowledgement is a blocker and names a fresh recovery; resume is offered only when the adapter and stored session support it. Key on the exit code.
- An `office` command exits 75 (EX_TEMPFAIL) and prints `office: install is being rebuilt, retry in 5 s (<ExceptionType>: <module>)` on stderr, with no traceback, when the install is replaced as the command starts (for example during `uv tool install --force`). Retry the same command after a few seconds, because nothing ran. A failure whose origin is outside the office package (a dependency that fails to import) is not a rebuild: the command exits 1 and prints its cause with a reinstall hint, naming `uv tool install --force --editable <checkout>` for a checkout or `uv tool install --force auto-office` for a release. If a reinstall is already running, the hint says to retry after it finishes. A replacement that breaks a command after it has started still raises a traceback; check the run before retrying it. The root wake supervisor records one `office.reinstalled` event once the install is healthy again, and that event wakes the root to resume the run. The event waits for a fresh-process probe that imports the CLI and resolves the version. While the supervisor's ticks keep failing it retries with backoff, up to 30 s between ticks.
- Exit 5 prints a `question:` line (dispatch, pane, question, options, answer command). Decide planning, scope, ordering and
  test detail yourself: amend the contract first if the answer changes it, then `office answer <task|dispatch> <n>` (a number
  presses that option in a selection widget; `office prompt` types text, which a widget ignores) or
  `office answer <task|dispatch> -- "<text>"`. Take it to the user only when it hints at requirements, authority, or an
  irreversible or external action.
- `office status` makes one `herdr agent list` call and prints a `blocker:` line for a live pane herdr reports `blocked` with no
  recorded question. `office wait` records it; status records nothing.
- Root wake (#922) is on by default. `office resume` from a bound, verified Claude Herdr root pane arms it; `--wake-off` turns it
  off for the run and it stays off across later plain resumes; `office resume --auto-wake` re-arms it. `--auto-wake` is still accepted. Outside Herdr, or when Herdr cannot verify the pane, resume prints a notice
  and arms nothing. `office status` prints `wake: armed`, `wake: off` or `wake: unavailable (<reason>)` on every non-terminal run.
  A turn that ends with work running and no wake armed is never woken, so arm `office wait` instead of ending the turn on an
  event-dependent step. Status ends with `next: office wait` in that state. `office context` repeats this rule.
- **Outside Herdr, a Claude Code root can now be woken natively (#940):** `office install --only claude --item turn.stop`
  installs the managed `Stop` command hook with `asyncRewake: true` (Claude Code must load/approve this hook), alongside
  the existing session-start and prompt-submit hooks. After `office resume` inside a *verified Claude process*,
  the root's normal Stop event starts a background, one-per-run listener to the Office event ledger; when a new
  authoritative action arrives it exits **2**, providing a scoped system reminder to the **existing** idle Claude session.
  No Herdr pane, agent restart, synthetic user prompt or model polling is used. `office status` reports
  `wake: armed (Claude native Stop/asyncRewake; arms on next Claude Stop)` or `listener active`; `--wake-off`
  disarms both transports. If the hook is not installed, this is a detached shell, the session is not bound,
  or the caller is an executor/subagent, wake remains unavailable and `next: office wait` is the fallback.
  The listener ends on a new user prompt, session rebind, run closure or hook timeout (24 hours); a later Stop
  re-arms it. A terminal/closed Claude session cannot be reawakened. Check `office status` if a host disables hooks.
  Native exit 2 means *notification issued*, not action completed; the root must read `office context` and obey
  all permission/review gates. See [root-wake](root-wake.md) for the safety contract and manual fallback.
- Review gates past a bound are classified once per episode (#919). `office status`, `office wait` and the root-wake watchdog
  record each episode as an orchestrator event, which wakes an armed root. `office wait` exits 0 on `review.queued_too_long`
  and `review.closed_unread`, and exits 3 on `review.stalled`:
  - `review.queued_too_long`: a task review still queued past `OFFICE_REVIEW_QUEUED_S` (default 900). Task reviews only; plan
    reviews show on the plan review status line. A queue nothing drains (owner `queued`) gets `office revoke <task>`, then
    `office review <task>` on the same revision. A live or unknown job gets `office wait` first.
  - `review.stalled`: a running reviewer past `OFFICE_REVIEW_NO_PROGRESS_S` (default 900) or `OFFICE_REVIEW_WALL_S` (default 3600).
    Next: as the stall line says.
  - `review.closed_unread`: a review closed past `OFFICE_REVIEW_UNREAD_S` (default 900) whose verdict has not been shown to the
    orchestrator. Next: `office context`, then act on the verdict. Every status (the root's `office status`, `office resume`, the
    session-start hook, and an `office wait` wake) prints up to three unread closed verdicts as `verdict shown:` lines, or the
    episode line past the bound, and marks only those read. The rest print on a later status. `office context` marks nothing read.
  - A healthy running review emits nothing.
- An executor idle 60s without submitting (or whose process died) is a stall: `office wait` exits 3, and the stall line names the
  dispatch, any refused-submit reason, its `pane-tail.txt` and the next command. Fix the blocker, then
  `office prompt T2 -- "<message>"` or follow that command. A worker refused as lease-lost, superseded-dispatch or
  task-paused is done: never prompt it to retry.
- Check suites share a host-wide cap (`verification.check_concurrency`). A quota wall blocks the worker without a relaunch;
  rerun after the reset or with `--as`. A check or test-runner timeout while host load exceeds twice the CPU count is
  UNAVAILABLE, not a failure: `office resume`.
- A task `checks:` command must install what its tests import (e.g. `uv run --extra visual ...`). A suite that skips every test
  exits 5 and fails the gate.
- Never run commands inside a task worktree yourself: tools leave files (`uv run` writes `uv.lock`) and `office submit` then
  refuses them as out of scope. Reproduce a check in a scratch copy.

## Planning inline

Start from the fetched remote default branch. Office pins that commit even when the caller's local main or detached checkout
lags it; an explicitly selected stale base refuses with the current `origin/<default>` remedy. Fetch first when newer upstream work is expected.

Run the four self-review lenses (`skills/office-submit`) over the plan and write what they surface into tasks' `accept:`
criteria. Plan the seams, not the internals: `scope:` is an ownership envelope (module or domain dirs plus their tests); name
exact files only where tasks collide or depend. Append-only registries several tasks touch (gate manifests, endpoint/grant lists,
policy maps, shared mocks) go under each task's `shared:`. Tasks that must land together share a `lane:`; lanes sharing an
outcome, a `converge:`; acceptance needing another lane's result, `accept_needs: T2`; a risky composition,
`integration_risk: high` (each adds one integrated review).

## Herdr agents

Inside Herdr, Office starts each dispatch as a real interactive agent in a pane beside yours and confirms the brief pointer
landed. A pane closes itself once its result is accepted, after saving `pane-final.txt`; a failed end keeps it open.
`office dismiss <T2|dispatch|--all>` closes kept panes; `OFFICE_KEEP_PANES=1` on `office dispatch` keeps them for debugging.

- A `launch` notice in `office status` means the pane agent never started (the dispatch ran headless) or the prompt never landed
  (re-prompt it). When startup fails before Herdr registers an agent, Office keeps the pane, snapshots its last screen, names
  recognized startup interstitials and prints the pane id.
- Before it types into a pane (the brief, `office prompt`, a review re-prompt) Office reads Herdr's own record of that pane: its cwd must be the
  dispatch's worktree, the agent kind must be the one the dispatch started, `herdr agent list` must name the dispatch's agent there, the
  reported session must match the recorded one, and no other dispatch may hold the pane's reservation. Each send re-checks the reservation
  under the pane lock, which is held for that one send only. Any mismatch sends nothing, starts no headless copy and prints
  `pane-mismatch: <why>; brief NOT sent`: read the pane, then `office revoke <task>`.
- opencode workers and reviewers get panes like every other harness with a Herdr kind: `opencode mini --model <slug>#<effort>` (a reviewer adds
  `--standalone --agent office-reviewer`, its per-dispatch permissions), labelled and identity-checked before the brief is sent. `mini` has no
  timeout of its own, so the pane watcher stops an opencode pane past `max_minutes` (60, or `OFFICE_WORKER_MAX_MINUTES`) as `timeout` and
  closes the pane, keeping `pane-final.txt`. A `Free usage limit reached` or `Rate limit exceeded` wall in the pane is classified from the pane
  tail like any pane agent's. Office reads opencode's session store (read-only) only to confirm the brief landed and to record the session id;
  a review is read from the reply file only. Without Herdr the headless `opencode run` form runs.
- `office revoke` restores a task to its accepted revision only when an amendment delivery alone demoted it. A task reopened for work still
  owed (open RECHECK or APPROVED-cleanup findings, a `restack:` pause, a hold for an amended dependency) stays reopened.
- Office presses Enter for a prompt left typed but unsubmitted. A notice saying it is still unsubmitted means
  `herdr pane send-keys <pane> Enter`, not a re-prompt, which would send it twice.
- To message a live worker or reviewer, run `office prompt <T2|dispatch> -- "<message>"`: it sends with `herdr agent prompt`,
  confirms it landed and presses Enter for one left typed. Never use `herdr pane run` or `pane send-text` on an agent pane;
  Claude takes their Enter as part of the paste and leaves the text unsubmitted.
- A headless worker (`process-fallback`, no pane) cannot be typed to: `office prompt` queues the message ("queued: ... runs
  headless; it sees this on its next office command"). It is not an amendment and needs no ack. A headless fallback shows in
  `office status` and `office wait` as "runs headless (herdr fallback): <why>". A `rerun --resume` that falls back headless
  resumes the recorded session when the adapter declares `headless_resume_argv`, else it says it started fresh.
- Claude's "Allow external CLAUDE.md file imports?" dialog is named in that notice and never answered by Office;
  `office doctor` warns when CLAUDE.md imports files outside the repo.
- To relaunch by hand: `office revoke T1`, then `office dispatch T1 --external` (plus `--as` for another model); it prints the
  `herdr pane run`, `herdr agent start` and `herdr agent prompt` commands. A prompt has landed when the agent reports `working`
  or its pane shows a running turn. agy reads `idle` mid-turn: judge an agy pane by its footer and `git status`.

## Review recovery, checks and stale dependents

- A review that returned no verdict (UNAVAILABLE, ATTENTION, INVALID_RESULT) blocks its gate with a runtime status, not a verdict. Re-run only
  that review of the same revision: `office review <T1|L-T1[:convergence|visual]> [--as <harness>/<model>[@effort] [--cli ARGV | --external]]`,
  `office rerun <task> --review-as <route>` or `office resume`. No executor starts and no round is spent. A pinned reviewer is kept unless `--as`
  re-pins it (checked against the reviewer role before any pin is written); without `--as` the next fallback route runs, and a re-run never
  resumes the earlier reviewer over a named or pinned route. A terminal run is refused.
- `office review L-T1:convergence|visual --report <file>` is the orchestrator reviewing a lane itself (visual: `--inspected <every screenshot>`);
  a v3.1 run takes `office review T1 --report <file>` for a task whose code review no reviewer answered (#326). Specialists come first. A v3.1
  task review is recorded degraded and non-independent and is never a waiver. Any of them is refused for the session that produced the work and
  inside a worker or reviewer dispatch.
- A failing task check is re-run on the revision's base commit. It is pre-existing only when it fails the same way there (same exit status and
  failures); never when its output names a changed file, when the revision changes no file, or on timeout. Under `convergence-v1` a pre-existing-only
  failure is a nonblocking note and the lane review still runs; on a v3.1 run the checks gate passes without cancelling code review, and a waiver of
  checks re-queues the reviews checks had cancelled.
- Re-accepting a task reopens its accepted dependents whose revision does not contain the new one: each is paused `restack: ...` and `office status`
  prints a rerun command supported by its stored session and adapter. If the original route is unavailable, inspect the route before selecting another.
  An amendment/redispatch reusing a task worktree restacks accepted dependencies before the executor starts (issue #696).
  If tracked WIP blocks restack, revoke the failed relaunch and run `office rerun <task> --fresh --stash-wip`:
  Office saves tracked/untracked WIP under its own `refs/office/wip/` ref (never the shared `refs/stash`), merges the dependency, and restores the WIP.
  A restoration conflict appends the WIP to `git stash list` and reports its immutable commit for recovery; the orchestrator must not run raw Git in the worktree.
  A conflicting restack leaves HEAD unchanged and tells the executor to merge every missing accepted dependency, resolve within its scope, and rerun checks.
  Integration refuses a stale composition with `INTEGRATION stale: <T> accepted on ... does not
  contain <dep> accepted on ...; restack <T> onto <dep>` instead of reporting a merge conflict.
- A multi-parent restack first selects the accepted dependency tips that contain the other parents. A dependency already merged
  by the worker still becomes the recorded scope-diff base. Shared-append verification uses that accepted base and skips paths the task never changed.

## Landing detail

- If the default branch moved after `office start`, run `office land --rebase` first: it re-composes the accepted work onto the new
  head and re-runs the run checks and one review of the rebase. A conflict refuses and prints the steps to compose by hand. When
  run checks fail and the default branch moved past the compose base, Office does this rebase itself once per new head.
- A task PR GitHub reports `CONFLICTING`/`DIRTY` (a branch-policy refusal is not a conflict and is not recovered) gets one recovery attempt per
  land. Office fetches the default branch and the task branch, then proves all of: the remote head is one Office accepted, restacked or recovered; the
  PR merges with the default branch without conflicts; and folding every open task onto it gives the reviewed integration tree. Only then does it
  push a merge commit to the task branch (a force-with-lease on the head it read), wait for GitHub to see it and merge pinned to that commit. A
  moved branch, a conflict, a tree mismatch, a failed fetch or push, or git older than 2.38 (`merge-tree --write-tree`) refuses with the reason and
  the by-hand steps; a refusal pushes nothing, and a timed-out push is reported as unknown, not as nothing pushed. The `pr.recovered` event records the
  heads and base.
- Office rewrites only the block above the PR body's `<!-- office:pr ... -->` line; put criteria that report data below it.
- If GitHub access becomes available after a run started with `--no-prs`, ask the user to authorize
  `office amend options --prs --quote "<user's exact words>" --confirm` before switching. Office records a
  `run.options_amended` event, enables subsequent task PRs and queues PR sync for already accepted work.
  Disabling PRs after a PR exists, a sync job was queued, or an executor has live PR instructions is unsafe
  and refused; changing gear/base/review gates is not a supported live option amendment.
- With task PRs off (local, no GitHub, `--no-prs`), push the integration branch it names, open a PR with `Closes #<issue>`, and
  `office close --handoff <pr-url>`. `office close --abandon "<reason>"` stops early; nothing is deleted until `office prune -f`.
- If `office land` stops after the task PRs merge, retry it: recorded merges are skipped, default-branch verification resumes,
  and a recorded successful deploy is not repeated. After it succeeds, run `office close`.
- `office close --landed-externally <merged-pr-url>` also recovers squash merges. Without original-commit ancestry, Office requires
  the current accepted integration and review gates, a fresh fetch proving the supplied merge is on the pinned default branch, and
  either every task's owned PR merged with its accepted head or exact equality with the reviewed integration tree. Historical Office
  restack/recovery heads need the same independent tree proof. A different tree never qualifies through an unchecked path subset.
  Office re-runs the approved run and task checks on the merged commit, records their output and the proof in the archive receipt,
  and refuses if checks fail or the accepted work, requirements, or gates change. Squash proof is separate from commit ancestry;
  it creates no authorization and does not merge, deploy, or close the issue. If neither proof applies, ask the user and add
  `--quote "<words>"` only for their explicit confirmation that the PR landed this run's work.
- Deploys run in a fresh checkout of the landed commit. Each deploy and verify step reports its cwd (`deploy ok: <cmd> (cwd <path>)`),
  and a failure names the same cwd, so a path a command expects is checked against that checkout.
- Before removing a deploy checkout, Office copies and verifies all untracked and ignored output into a private
  `deploy-artifacts/<run>/<checkout>-<id>/` archive under the state home. The deploy log, close output and archive receipt name it;
  `manifest.json` maps the original checkout paths to files, hashes, modes and symlink targets. Links are copied without following
  them. These archives survive `office prune`; retain them for your rollback policy. An archive failure keeps the checkout and
  refuses cleanup. `office close` also preserves output left by an interrupted deployment before becoming terminal.
- `office land --detect` warns when a deploy command names a repo path the fresh checkout lacks (gitignored or untracked, e.g. `.env`
  or `myaccount/.env` passed to a sourced script). Fix it by listing the path under `deploy.env_files` in the config: Office copies each
  listed file into the deploy checkout with its mode preserved, never stages or commits it, and refuses an entry outside the repo.

## Root context snapshot

`office start` resolves the base and pins configuration from the caller's checkout, then records the durable repository root
identified by the shared Git directory. A removable linked caller such as `.claude/worktrees/agent-*` is not the run's Git cwd.
Inline plan drafts live under the recorded root; use the path printed by `start` and `status` when operating from a linked checkout.
Dedicated planners continue to author their drafts in their own dispatch worktrees.
For a linked caller with a separate external Git directory, that common directory is the durable Git anchor; inline drafts live
there too. A primary checkout using `--separate-git-dir` keeps its actual working root. Pinned configuration does not change when
the durable root has different config files; status reports the difference without assuming anyone edited the files.

When an older run's recorded repository disappears, `office status`, `wait` and `resume` recover to the primary/shared-Git root
only after proving it has the recorded common Git directory. The base, pinned policy and review records stay unchanged, and a
`run.repository_reanchored` event records the old and new paths. If no such root is available, `repository-unavailable` names the
missing path and tells you to restore it before `office resume`. Unsubmitted edits in a deleted caller checkout cannot be recovered
from Office; submitted plans and revisions remain in the run's authority and evidence.

A capture whose Playwright browser executable is missing reports `CAPTURE_BLOCKED` immediately, with no visual verdict or review
round spent. Follow its printed command to install the matching browser, verify with `office doctor`, then `office resume` to
capture again. The required screenshots still block landing; changing reviewers or amending the visual contract does not install
the browser. Browser installation and a live capture remain host-dependent verification, never inferred from a synthetic test.

`office context [--json] [--since SEQ]` prints a bounded, read-only snapshot of the run for a root that restarted or compacted: goal,
requirements and plan versions with hashes, open tasks, pending gates, blocking findings, recent amendments and authorizations,
referenced evidence with its hash status, the `next:` action, and the event cursor (`--since` lists up to 20 later events). It is
assembled from runs.db on each call and writes nothing; `office status` stays the lean default. Every `stale:` line is a reference
that no longer matches: a PLAN.md draft that differs from the stored plan, evidence that is missing or re-hashes differently, a run
pinned to another release line, or state that changed while the snapshot was read (rerun it).

## Orchestrator economics (opt-in)

Usage collection is off by default (`economics.collect: false`); while off nothing reads session logs or writes usage rows,
and `office inspect economics` says collection is disabled. `office config economics.collect true` opts in, then
`office economics ingest` (on demand, never from a hook) reads the token/cache usage each harness recorded in its own transcripts
for this run's dispatches and the bound root session into runs.db `usage_events`. Root-session turns outside the run's binding
window are stored as `outside` and excluded. Input, output, cache-read and cache-write tokens are separate; a field the harness did
not report is unknown, never 0. Codex input includes its cached input, which is subtracted once. Transcript cost is `unknown`
(no price table); `--file <jsonl>` imports normalized rows with `cost_kind` actual, estimated-nominal, quota or unknown.
`office inspect economics` shows usage by role and phase, cache share, cold resumes (only a resumed dispatch whose first measured
turn wrote cache and read none; a long gap alone is not evidence), and coverage. `office config economics.collect false` stops it.

## Bug reports

Reporting an Office defect is the orchestrator's job, not the runtime's. The runtime prints a non-blocking `bug?:` line when an outcome looks
like a defect, and `office self-improve` prints the workflow plus the run's current `bug?:` signals. It starts nothing and records nothing. Follow `skills/auto-self-improve/SKILL.md`: dedupe, sanitize, then `gh issue create` on Hikari9/auto-office,
directly or through a cheap read-only subagent. Issues only; never fix the bug or open a PR as part of it.

## Historical material

Everything under `references/OFFICE-SKILLS-V3-*.md`, `docs/v3-*.md` and the 3.0 spokes (`skills/auto-planning`,
`auto-execution`, `auto-review`, `auto-verification`, `auto-loop`) is 3.0-era reference. It explains where rules came from. It is
not operational policy for 3.1+ runs, which follow `office status` and `docs/review-convergence.md`.

## Shadow delegation and efficiency inspection (#502)

Every dispatch of multiple tasks now writes a compact `delegation.shadow` **operational decision event** to the existing run event ledger before fan-out. It compares direct task dependencies and ownership-envelope overlap, records the actual serial/parallel request and proposed independent/cohesive strategy, and explicitly says when full-run economics or cache policy are unknown. This event does not change dispatch order, inline/delegated execution rules, routing, protected work, user overrides or review. It is not a model-use or token collector, and requires no additional hook I/O.

`office inspect efficiency` reads observed Office-attributed usage and recent shadow events on demand. Economics collection remains opt-in and off by default (`economics.collect: false`); neither this command nor shadow dispatch reads transcripts, scans sessions, estimates billed dollars or enables telemetry. A root percentage is the share **of observed token components**, never proof of entire-run cost. Missing fields, outside-Office turns, actual USD, nominal estimates and subscription quota remain distinct. Unknown review/rework/admin costs are not silently zero.

For controlled comparisons, `python eval/v31/compare_economics.py cases.json` accepts a JSON array with matched `pair_id` and `arm` (`baseline` or `treatment`), identical `task_shape`, `harness` and `billing_mode`, `verified_completion: true`, non-negative `integrated_defects`, measured values in `metrics`, and explicitly verified `complete_metrics`. Metrics are withheld unless both arms complete; reported actual USD comparisons require API billing and `cost_kind: actual`. It needs at least three complete, matched pairs with non-worse verified defects before identifying an improvement as claim-ready. **No real matched run dataset has been supplied in #502; no efficiency improvement is claimed yet.**

## Executor context cap (#939)

The per-executor cap is separate from the orchestrator's ctx-high notice. Pinned
`executor_context.enabled` (default true) and `executor_context.threshold_tokens`
(default 200000; 0 disables) live in `config/config.default.yaml` and ordinary run config.
An invalid threshold falls back to 200000 and `office status` says so.

Measurement: the supervisor checks attributable Claude and Codex session transcripts every
15 seconds while the executor runs. Claude
uses the last assistant message's input plus cached-input usage, excluding sidechains. Codex
uses the last turn's input and output count, never cumulative lifetime totals. Unobserved or
unsupported harnesses are not guessed and cannot claim an enforced cap.

Signal: crossing the cap writes one `executor.context_cap` event (which wakes the root) and a
`signaled` marker on the executor dispatch. Office takes no other action. The marker clears when
the session drops below the cap; the next crossing is a new episode.

Decisions (the root's only way to act):

- `office executor-context <task> rotate`: refused when rotation is disabled for the task, the
  task has no live executor, the live writer changed while the snapshot was taken, or a rotation is
  already requested. An executor session (`OFFICE_DISPATCH_ID` set) is always refused. One transaction writes a
  `requested` handoff snapshot (task state, worktree HEAD, git status and diff stat, existing
  task commits as evidence to verify, remaining ACCEPT items, open findings) and enqueues a
  deduplicated `rotate_executor` job. The snapshot never asserts completion. A private copy is
  at the run's `dispatches/<old-id>/context-handoff.json` (0600).
- `office executor-context <task> continue`: acknowledges a `signaled` marker. Refused when no
  signal is pending.
- `office executor-context <task> threshold <tokens>`: records a per-task cap as an
  `executor.threshold` event. Whole tokens only; 0 disables rotation for the task.
- Repo-wide: `office config --repo executor_context.threshold_tokens <tokens>`. A running run
  keeps the policy it pinned at start, so use the task form for the current run.

Opt-outs: `executor_context.threshold_tokens: 0` or `executor_context.enabled: false` disables
the signal and rotation. `enabled: false` also overrides a per-task threshold.

If a requested rotation cannot proceed (the task moved, or the executor submitted), its handoff
becomes `skipped` with an `executor.rotate_skipped` event, which wakes the root; decide again.

The rotation job stops the old executor's process tree and verifies it has exited before it
creates a fresh dispatch on the same task, branch and worktree. The replacement brief carries the
handoff. Dependency restacking is skipped so uncommitted WIP stays put. The exhausted session is
never resumed, and the stop is not counted as a route failure or a retry. If the stop cannot be
verified, the task is blocked with a visible reason and no replacement launches. A submitted or
superseded executor is not restarted.

`office status` shows the pending signal and each requested, restarted, or blocked handoff. `--json`
exposes them as `executor_handoffs`. The DB row is authoritative; the file is a projection, and a
file-write failure never causes a second launch.
