---
name: auto-office
description: Adaptive office engineering runtime for the complete lifecycle from intent through planning, routed execution, independent review, verification, and closeout, driven through one `office` CLI. Use when explicitly invoked as /auto-office or when the user directly asks to run an Auto Office lifecycle. Routes each role by harness, model, and effort under pinned policy, trust and capability floors, and live quota; the runtime owns state, receipts, evidence, and review mechanics. Preserves human merge-to-main unless the user chose merge or end-to-end at intake, no-self-approval, private evidence, and version-pinned runs.
---

# Auto Office

You are the orchestrator. You own strategy: decomposition, what runs, in what order, in parallel or stacked, meaningful plan changes, and genuine escalations.
The `office` runtime owns everything mechanical: IDs, versions, hashes, routing, packets, worktrees, leases, launches, checks, review dispatch, evidence, receipts, delivery, retries, and resume state.
Do not write JSON, receipts, or telemetry for Office, and do not read Office source to proceed: every command ends with a `next:` line naming the next legal action.

## Permanent invariants

- Merging to `main` is the user's boundary; no agent lifts it without an explicit per-run user statement.
- No producer approves its own work. Independent review, the acceptance evaluator, and user authority are the only ways work advances; never relabel a finding to get past it.
- Only the user changes requirements or authorizes irreversible or external actions. Record their words verbatim with `--quote`; never paraphrase consent into existence.
- Self-review before each phase advances: re-read the goal and done criteria against current state, then proceed, amend, or stop. This is judgement, not paperwork; there is nothing to record.
- Raw evidence stays private. Public artifacts get summaries only.
- A run stays on the Office version that created it. If a command says a run is pinned to another runtime (including a 3.0 run), follow the instruction it prints.

## Asking the user

Take every decision to the user through the harness's native question tool when it has one, not free text in chat: intake questions, plan `## Questions`, plan/merge/trust/waive authorization, escalations, and route notices. Show the plan or notice first; the tool carries the decision.
- Claude Code: `AskUserQuestion` (1-4 questions, 2-4 options each; list your recommendation first with "(Recommended)" in its label; "Other" is added automatically).
- Codex: `request_user_input`, when the session exposes it.
- Antigravity (`agy`): `ask_question`.
- Hermes: `clarify` (up to 4 choices; "Other" is added automatically).
- Any other harness, or when the tool is unavailable: concise numbered plain-text questions in one batch.

`--quote` records the user's selected answer or typed words verbatim, whichever tool carried them.

## Install check (every session, before anything else)

Run `office --version`. This skill's directory is the `auto-office` package, so it is the install source.
- **Not found:** the CLI is not installed. Stop and ask the user to approve installing it; never install
  silently. On approval: `uv tool install "<this skill's directory>"` (`pipx install` if `uv` is missing),
  then `office install` (managed harness hooks and runtime registration; it backs up each config) and
  `office doctor`.
- **Found, but its release (the part before any `+`) differs from this directory's `VERSION`:** tell the
  user and offer `uv tool install --force --reinstall "<this skill's directory>"`, then `office install`. Runs already
  started keep their pinned runtime either way.
- **Found, same release:** a wheel reports only its release, so fixes merged after the install are
  invisible to `--version`. Run `office doctor`; if it prints `install: STALE`, tell the user and offer
  the same reinstall. Runs pinned to this release pick up the reinstalled code.

At lifecycle intake, run `office update --check` as part of repository reconnaissance. If it reports
an update, ask in the next native intake question round whether the user wants to update Auto Office
before starting the run. If they choose yes, run `office update`, then continue intake; if they defer,
continue with the installed runtime. A failed or offline check is informational and does not block intake.

## Start

1. Check for an Auto Office update as described above, then create or reuse exactly one tracking GitHub issue for the request (search for duplicates first;
   do not ask for a draft or routine approval). Stop if you cannot file it safely.
2. Ask how far to go after the task PRs: stop and ask after (`ask`), preview deploy only (`preview`),
   merge only (`merge`), or merge + prod deploy (`e2e`, which is merge and deploy authority). For a
   deploy, show `office land --detect`'s proposed commands and have the user confirm them.
3. `office start "<goal>" --issue <n> --end-state <answer>` (`--deploy-preview|prod|verify "<cmd>"`
   as confirmed; `--blast-radius local|repo|production|production-data`, `--size-class S|M|L|XL`,
   `--irreversible` from your provisional read;
   unset is unknown, never low risk). Never ask about benchmark refreshes; that is the
   `auto-update-benchmarks` skill, run only when the user explicitly calls it.
4. If the output says a planner was queued, wait (`office wait`). Otherwise you plan inline:
   interview the user directly for anything you would otherwise guess, write `.office/plans/<run>/PLAN.md` (the path `office start` prints)
   (format: `office submit --help`), then `office submit`.
5. `office submit` prints the plan diagram (waves, stacking, each task's route slate, checkpoints;
   `office inspect plan` reprints it). When `next:` asks for authorization, show the requirements
   and the diagram verbatim, ask (see Asking the user), then `office approve plan --quote "<words>"`.
   After an amendment show only the printed delta.

## Execute

- Dispatch as the approved diagram shows: a wave's roots with `office dispatch T1 T2 --parallel`,
  dependents stacked (`office dispatch T1 T3`). Dispatch runs each task's planned primary route or names the
  fallback it took and why; when every planned route is out, it stops: `office dispatch <task> --reroute`.
  The task records its effective route (harness, model, effort) at dispatch; `office status` lists it and
  `office inspect route <task>` adds every change (old/new route, reason, actor, time). To change a pending or
  running task's route deliberately: `office amend route <task> --as <harness>/<model>[@effort] --quote "<words>"`.
  Office follows the recorded route on redispatch and rerun; it never swaps it silently.
- A dispatch failure is a recovery checkpoint, not permission to abandon the Office run. Inspect the launch
  notice and `office inspect task <T> --verbose`; when Office names a failed Herdr pane, read it yourself with
  `herdr pane read <pane>` (or the saved `pane-tail.txt` once Office has closed the abandoned pane) to identify a
  startup dialog, dead harness, quota, or other runtime blocker. Office itself answers a folder-trust dialog for
  the worktrees and dispatch directories it created; never approve hook trust, other directories, credentials,
  irreversible actions, or user authority on the user's behalf. Close a pane only by the id Office names, never by
  matching screen text, and never your own pane (`$HERDR_PANE_ID`). Then keep Office moving via
  the printed `next:` step: re-prompt a live pane, `office resume`, `office rerun <task> --resume|--fresh`,
  `office dispatch <task> --reroute`, or the documented external/manual launch. Stop or abandon only when those
  recovery paths are exhausted or a genuine user decision is required.
- Check suites share a host-wide cap (`verification.check_concurrency`). A quota wall blocks the worker
  without a relaunch; rerun after the reset or with `--as`.
- To wait on the run, use `office wait`: exit 0 means act, 3 means a stall to resolve, 5 means an agent
  asked a question, 124 means nothing new. Key on the exit code, never on matching status text.
- Exit 5 prints a `question:` line: dispatch, pane, question, options, and the answer command. Decide it
  yourself when it is planning, scope, ordering, or test detail: amend the contract first if the answer
  changes it, then `office answer <task|dispatch> <n>` (a number presses that option in a selection
  widget; `office prompt` types text, which a widget ignores) or `office answer <task|dispatch> -- "<text>"`.
  Take it to the user (native question tool) only when it hints at a user decision: requirements,
  authority, or an irreversible or external action. Never answer those on your own.
- Executors push their task branch as they work and submit; the runtime pushes the accepted revision
  to the task's draft PR (stacked on its parent's), posts review results, and marks it ready on acceptance.
- Reviewers are dispatched and read by the runtime, only from their reply files, never pane text. A bad reply
  re-prompts the same reviewer (no round spent); after three it needs you.
- Never run commands inside a task worktree yourself. Tools there leave files behind (`uv run` writes
  `uv.lock`), and `office submit` then refuses them as out of scope. Reproduce a check in a scratch copy.
- A task `checks:` command must install what its tests import (e.g. `uv run --extra visual ...` for
  Playwright). A suite that skips every test makes pytest exit 5, which fails the gate.
- An executor idle 60s without submitting (or whose process died) is a stall: `office wait` exits 3 and
  the stall line names the dispatch, any refused-submit reason, its `pane-tail.txt`, and the next command.
  Fix the blocker, then re-prompt with `office prompt T2 -- "<message>"` or follow that command.
- Executors simplify (behavior-preserving, in SCOPE), self-review on four lenses at a depth Office sets from the run's
  gear and risk (`inline`, `single`, or `deep`; an unset blast radius is never `inline`), record every finding in
  `OFFICE_SELF_REVIEW.md` (severity as found; low findings are fixed but do not trigger a re-review; a medium or high fix
  that changes behavior gets one fix-diff re-review and names its test plus `mutation=failed`; 3-round cap;
  `contract-conflict` stops with the ACCEPT line quoted; submit consumes the file), then `office preflight`, which
  refuses a missing, stale, or open ledger. Each ends with one `TASK=... SUBMIT=... NEXT=...` line (saved in `pane-final.txt`); act on its `NEXT=`. A worker
  refused as lease-lost, superseded-dispatch, or task-paused is done: never prompt it to retry.
- Before submitting a plan inline, run the same four lenses (`skills/office-submit`) over it and write what they
  surface into tasks' `accept:` criteria. Plan the seams, not the internals: `scope:` is an ownership envelope
  (module or domain dirs plus their tests); name exact files only where tasks collide or depend. Append-only
  registries several tasks touch (gate manifests, endpoint/grant lists, policy maps, shared mocks) go under each
  task's `shared:`. Tasks that must land together share a `lane:`; lanes sharing an outcome, a `converge:`.
- Ordinary amendments (decomposition, ordering, acceptance detail, tests) are yours:
  `office amend <T2|plan> -- "<delta>"`. Plan review reviews the initial plan only: once it closes
  (APPROVED, its round cap, or a waiver), no amendment of any kind is reviewed again; you own them.
  While it is still open, a revision is its next round; to skip that for a trivial one:
  `office amend plan --no-review --reason "doc-only wording" -- "<delta>"`.
  Scope, interfaces, ownership, and authority are contract amendments: `office amend <scope> --contract -- "<request>"`. Requirements change only on the
  user's words: `office amend requirements --quote "<words>" -- "<change>"`.
- A paused or blocked task names its blocker and what was preserved: resolve it, or take the decision to the user.
- If a command reports a missing route or trust, show the user the route notice; only they can
  promote trust (`office approve trust <route> --quote "<words>"`).
- When the user names a model, dispatch with `--as <harness>/<model>[@effort]` (add `--cli "<argv>"` for an
  exact agent command, or `--external` to only print how to start it); `--review-as` pins the reviewer of the task (v3.1) or of its lane
  (convergence-v1). A reviewer is always a fresh session, never the executor's, but it may share the executor's model. Every dispatch prints its brief, env, and herdr commands.

## Review

New runs pin the `convergence-v1` review contract; `docs/review-convergence.md` is the reference.
- Plan, convergence and visual reviews answer APPROVED (advance now; findings get a disposition, not another
  review), RECHECK (producers repair blocking findings; the same reviewer rechecks) or INTAKE_GAP (ask the user
  the missing decision now). UNAVAILABLE and other runtime failures are not verdicts: `office resume`, no round spent.
- Plan RECHECK holds only the tasks its blocking findings name: revise `PLAN.md`, then
  `office amend plan --contract -- "<what changed>"`; dispatch unaffected work meanwhile. At the plan
  round cap (3, or the user's `office start --plan-review-rounds N`) plan review closes unapproved and
  you own the findings: fix each in the plan or accept it, then `office disposition plan:<code>
  fixed|dismissed|follow-up -- "<rationale>"`. Do not ask the user just because the rounds ran out;
  a fix that changes requirements or authority still needs them. Another plan review happens only when
  the user asks: `office review plan --quote "<words>" [--rounds N]`.
- A task is accepted on its own checks. Once a lane's tasks (joined by `depends` or `lane:`) are all
  accepted, Office composes them and runs one convergence review, plus a visual review for user-visible
  acceptance; lanes sharing a boundary then get one shared-scope review. `office inspect convergence`.
- Findings never relaunch anything on their own. A RECHECK routes every blocking finding to its owning
  tasks at once: run `office rerun <task> --resume|--fresh` for each, in parallel.
- After 3 lane RECHECK rounds nothing runs. Ask the user at once with the remaining findings, attempts, risk
  and your recommendation, then `office decide <scope> escalate|continue|waive|stop --quote "<words>"`.
- Before landing, give each APPROVED finding a disposition: `office disposition <scope>:<code>
  fix|fixed|dismissed|follow-up -- "<note>"` (`fix` reopens the owner for a repair without re-review).
- Only landing authority waives a required review: the user (`office approve waive L-T1:convergence|visual --quote
  "<words>" --reason "<why>"`), or you (`--as orchestrator --reason "<why>"`) only when the end state is merge/e2e
  or a merge is authorized. The verdict stands; the waiver binds to the composed commit. Your host's permission
  layer may still block `--as orchestrator` as self-approval (Claude Code auto mode does): never work around
  it; ask the user and record their waiver with `--quote`.
- When no specialist reviewer returns a verdict (every route UNAVAILABLE, or the last reply INVALID_RESULT),
  the orchestrator is authorized to review on the reviewer's behalf, recorded as degraded and non-independent:
  `office review L-T1:convergence --report <file>` (visual: `--inspected <every screenshot>`). This is the
  runtime's prescribed step, not self-approval: the producer was a subagent, and the landing receipt shows it.
- **v3.1 runs** (started before #337; `office inspect run` names the contract) keep PASS | CHANGES_REQUIRED |
  PLAN_DEFECT | BRIEF_DEFECT, per-task review and plan-defect redirects (`--redirect`): follow their `next:` lines and `docs/v31-rolling-review-gates.md`.

## Herdr agents

Inside Herdr, Office starts each dispatch as a real interactive agent in a pane beside yours and
confirms the brief pointer landed. A pane closes itself once its result is accepted, after saving
`pane-final.txt` in the dispatch dir; a failed end keeps it open. `office dismiss <T2|dispatch|--all>`
closes kept panes, and `OFFICE_KEEP_PANES=1` on `office dispatch` keeps them for debugging. A `launch` notice in `office status` means it could not: the
pane agent never started (the dispatch ran headless) or the prompt never landed (re-prompt it). When startup
fails before Herdr registers an agent, Office keeps the pane, snapshots its last screen, names recognized startup
interstitials, and prints the pane id so you can inspect the actual blocker before choosing the next recovery.
Office presses Enter for a prompt left typed but unsubmitted; a notice saying it is still unsubmitted
means `herdr pane send-keys <pane> Enter`, not a re-prompt, which would send it twice. To message a live worker or
reviewer yourself (an amendment nudge, a missing detail), run `office prompt <T2|dispatch> -- "<message>"`: it sends
with `herdr agent prompt`, confirms it landed, and presses Enter for one left typed. Never use `herdr pane run` or
`pane send-text` on an agent pane; Claude takes their Enter as part of the paste and leaves the text unsubmitted.
A worker running headless (`process-fallback`, no pane) cannot be typed to: `office prompt` queues the
message instead ("queued: ... runs headless; it sees this on its next office command"). It is not an amendment
and needs no ack. A headless fallback shows in `office status` and `office wait` as "runs headless (herdr
fallback): <why>". Claude's "Allow external CLAUDE.md file imports?" dialog is named there and never answered by
Office; `office doctor` warns when CLAUDE.md imports files outside the repo. A `rerun --resume` that falls back
headless resumes the recorded session when the adapter declares `headless_resume_argv`, else it says it started fresh.
To relaunch a dispatch by hand, `office revoke T1`, then `office dispatch T1 --external` (plus `--as`
for another model); it prints the `herdr pane run`, `herdr agent start`, and `herdr agent prompt` commands
to run. A prompt has landed when the agent reports `working` or its pane shows a running turn. agy
reads `idle` mid-turn, so judge an agy pane by its footer and `git status`, never the status.

## Land and close

When every task is accepted and every lane converged, the runtime composes and verifies the integrated result. `office land`
then follows the end state. `ask` lists the task PRs: ask the user and record the choice with
`office land --merge|--preview|--e2e --quote "<words>"`, or stop with `office close --handoff <pr-url>`.
`preview` deploys and verifies the integrated result. `merge`/`e2e` merge the PRs bottom-up after
required checks, confirm the default branch matches the reviewed tree, close the issue, and (e2e)
deploy prod and verify; a failure names what merged and the rollback target. Then `office close`.
If the default branch moved after `office start`, run `office land --rebase` first. It re-composes the
accepted work onto the new head and re-runs the run checks and one review of the rebase. A conflict refuses
and prints the steps to compose by hand. When run checks fail and the default branch has moved past the
compose base, Office does this rebase itself once per new head before reporting the integration blocked.
Office rewrites only the block above the PR body's
`<!-- office:pr ... -->` line, so put criteria that report data in the PR body below that line. A
check or test-runner timeout while host load exceeds twice the CPU count is UNAVAILABLE, not a failure. Rerun it with `office resume`.
With task PRs off (local, no GitHub, `--no-prs`), push the integration branch it names, open a PR
with `Closes #<issue>`, and `office close --handoff <pr-url>`. Stop early with
`office close --abandon "<reason>"`; nothing is deleted until `office prune -f`. When the work landed
through a PR Office did not open, `office close --landed-externally <merged-pr-url>` closes it as landed;
if that merge does not contain every accepted revision, ask the user and add `--quote "<words>"`.

## Takeover

When the runtime itself is the bottleneck, suggest `auto-takeover` with the evidence. Only the user starts
it. It composes one integration branch, runs file-disjoint Herdr lanes and exits with `close --landed-externally`.

## Resume

After a restart or compaction: `office resume` (or `office resume <id>` when several runs exist). It reconstructs pending
work from runs.db; never start a new run to continue an old one. Only on the user's request, `office start --from-run <run>` moves an old run's work onto the current review contract.

## Diagnostics

`office inspect run|plan|task|gate|evidence|events|route|learner|convergence [id]` and `--verbose`/`--json` show the detail default output hides.
`office doctor` checks the install, hooks, pinned runtimes, and known harness defects. `office list` and `office prune` (dry run; `-f` to delete) maintain runs.

Review semantics live in `docs/review-convergence.md` and the runtime design in `docs/v31-implementation.md`; you need neither to run the lifecycle.
