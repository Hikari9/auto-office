---
name: auto-office
description: Adaptive office engineering runtime for the complete lifecycle from intent through planning, routed execution, independent review, verification, and closeout, driven through one `office` CLI. Use when explicitly invoked as /auto-office or when the user directly asks to run an Auto Office lifecycle. Routes each role by harness, model, and effort under pinned policy, trust and capability floors, and live quota; the runtime owns state, receipts, evidence, and review mechanics. Preserves human merge-to-main unless the user chose merge or end-to-end at intake, no-self-approval, private evidence, and version-pinned runs.
---

# Auto Office 3.2

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

## Start

1. Create or reuse exactly one tracking GitHub issue for the request (search for duplicates first;
   do not ask for a draft or routine approval). Stop if you cannot file it safely.
2. Ask how far to go after the task PRs: stop and ask after (`ask`), preview deploy only (`preview`),
   merge only (`merge`), or merge + prod deploy (`e2e`, which is merge and deploy authority). For a
   deploy, show `office land --detect`'s proposed commands and have the user confirm them.
   In the same question, offer an opt-in benchmark refresh (default off): if the catalog lacks
   scores for a model or effort, one low-cost background subagent fetches them during the run.
3. `office start "<goal>" --issue <n> --end-state <answer>` (`--deploy-preview|prod|verify "<cmd>"`
   as confirmed; `--benchmark-refresh` only if the user opted in; `--blast-radius`, `--size-class`,
   `--irreversible` from your provisional read; unset is unknown, never low risk).
   With the opt-in, run `office benchmarks brief`; if it prints a brief, start one background
   subagent on the smallest capable model at low effort with it and keep working. It submits its
   result with `office benchmarks submit`; a rejected delta changes nothing and blocks nothing.
4. If the output says a planner was queued, wait (`office wait`). Otherwise you plan inline:
   interview the user directly for anything you would otherwise guess, write `.office/plans/<run>/PLAN.md` (the path `office start` prints)
   (format: `office submit --help`), then `office submit`.
5. `office submit` prints the plan diagram (waves, stacking, route preview and why, checkpoints;
   `office inspect plan` reprints it). When `next:` asks for authorization, show the requirements
   and the diagram verbatim, ask (see Asking the user), then `office approve plan --quote "<words>"`.
   After an amendment show only the printed delta.

## Execute

- Dispatch as the approved diagram shows: a wave's roots with `office dispatch T1 T2 --parallel`,
  dependents stacked (`office dispatch T1 T3`). A `route differs from the plan preview` line names why.
- Lanes come from file disjointness, not plan waves. List append-only registries several tasks touch
  (gate manifests, endpoint/grant lists, policy maps, shared mocks) under each task's `shared:`; they
  never serialize tasks or count as double-scope ownership. Scope tests a change predictably breaks.
- Check suites share a host-wide cap (`verification.check_concurrency`). A quota wall blocks the worker
  without a relaunch; rerun after the reset or with `--as`.
- To wait on the run, use `office wait`: exit 0 means act, 3 means a stall to resolve, 124 means nothing
  new. Key on the exit code, never on matching status text.
- Executors push their task branch as they work and submit; the runtime pushes the reviewed revision
  to the task's draft PR (stacked on its parent's), posts verdicts, and marks it ready on acceptance.
- Reviewers are dispatched and read by the runtime, only from their reply files, never pane text.
  A bad reply re-prompts the same reviewer; after three it needs you.
- Never run commands inside a task worktree yourself. Tools there leave files behind (`uv run` writes
  `uv.lock`), and `office submit` then refuses them as out of scope. Reproduce a check in a scratch copy.
- A task `checks:` command must install what its tests import (e.g. `uv run --extra visual ...` for
  Playwright). A suite that skips every test makes pytest exit 5, which fails the gate.
- An executor idle 60s without submitting (or whose process died) is a stall: `office wait` exits 3 and
  the stall line names the dispatch, any refused-submit reason, its `pane-tail.txt`, and the next command.
  Fix the blocker, then re-prompt with `office prompt T2 -- "<message>"` or follow that command.
- Findings never relaunch anything on their own. When `next:` says a task's findings wait for you, run
  `office rerun T2 --resume` (the same harness session, in a fresh pane) or `office rerun T2 --fresh`;
  resume refuses with the reason and the `--fresh` command when the session cannot be reopened.
- After a first plan review of CHANGES_REQUIRED: edit the run's `PLAN.md`, run
  `office amend plan -- "<what changed>"`, then dispatch eligible work immediately; the re-review
  runs concurrently. A PLAN_DEFECT blocks its scope until an independent reviewer clears it.
- A PLAN_DEFECT means a requirement or assumption the plan rests on is unachievable or wrong;
  CHANGES_REQUIRED is only a plan amendment. On a defect, trace it to the requirement or assumption
  behind it. A plan-only cause (a task split, an ordering) is a normal `--contract` fix. Otherwise
  ask the user (native question tool) how to redirect that requirement, revise the plan, then
  `office amend plan --contract --redirect P3 --root-cause "<requirement>" --quote "<words>"
  [--requirement "<new requirement>"] [--reviewer same|fresh] -- "<fix>"`. The redirect resets the
  plan-review budget. `--requirement` records r(n+1), which the same quote authorizes. Choose
  `--reviewer same` (resumes the reviewer that raised it) when its context helps judge the fix, and
  `fresh` (the default, another route) when its framing rested on the old requirement. A dedicated
  planner asks the user in its own pane when it can, then runs `office submit --redirect ...`.
  Otherwise it lists the question under `## Questions`, and you ask the user and redirect. If the
  user judges the defect wrong, run `office approve waive P3 --quote "<words>"`.
- Ordinary amendments (decomposition, ordering, acceptance detail, tests) are yours:
  `office amend <T2|plan> -- "<delta>"`. Scope, interfaces, ownership, and authority are contract
  amendments: `office amend <scope> --contract -- "<request>"`. Requirements change only on the
  user's words: `office amend requirements --quote "<words>" -- "<change>"`.
- A paused or blocked task names its blocker and what was preserved. Resolve it, or take the
  decision to the user; after exhausted convergence the user may accept a named gap with
  `office approve waive T2:<gate> --quote "<words>"`. When a visual gate is UNAVAILABLE and the user has a
  reviewer run it by hand, record that review file as the gate result:
  `office approve visual T2 --by <harness>/<model>[@effort] --report <file> --quote "<words>"` (never the
  producer's model family).
- If a command reports a missing route or trust, show the user the route notice; only they can
  promote trust (`office approve trust <route> --quote "<words>"`).
- When the user names a model, dispatch with `--as <harness>/<model>[@effort]` (add `--cli "<argv>"` for an
  exact agent command, or `--external` to only print how to start it) and `--review-as` to pin the code
  reviewer, which must be a different model family. Every dispatch prints its brief, env, and herdr commands.

## Herdr agents

Inside Herdr, Office starts each dispatch as a real interactive agent in a pane beside yours and
confirms the brief pointer landed. A pane closes itself once its result is accepted, after saving
`pane-final.txt` in the dispatch dir; a failed end keeps it open. `office dismiss <T2|dispatch|--all>`
closes kept panes, and `OFFICE_KEEP_PANES=1` on `office dispatch` keeps them for debugging. A `launch` notice in `office status` means it could not: the
pane agent never started (the dispatch ran headless) or the prompt never landed (re-prompt it).
Office presses Enter for a prompt left typed but unsubmitted; a notice saying it is still unsubmitted
means `herdr pane send-keys <pane> Enter`, not a re-prompt, which would send it twice. To message a live worker or
reviewer yourself (an amendment nudge, a missing detail), run `office prompt <T2|dispatch> -- "<message>"`: it sends
with `herdr agent prompt`, confirms it landed, and presses Enter for one left typed. Never use `herdr pane run` or
`pane send-text` on an agent pane; Claude takes their Enter as part of the paste and leaves the text unsubmitted.
To relaunch a dispatch by hand, `office revoke T1`, then `office dispatch T1 --external` (plus `--as`
for another model); it prints the `herdr pane run`, `herdr agent start`, and `herdr agent prompt` commands
to run. A prompt has landed when the agent reports `working` or its pane shows a running turn. agy
reads `idle` mid-turn, so judge an agy pane by its footer and `git status`, never the status.

## Land and close

When every task is accepted the runtime composes and verifies the integrated result. `office land`
then follows the end state. `ask` lists the task PRs: ask the user and record the choice with
`office land --merge|--preview|--e2e --quote "<words>"`, or stop with `office close --handoff <pr-url>`.
`preview` deploys and verifies the integrated result. `merge`/`e2e` merge the PRs bottom-up after
required checks, confirm the default branch matches the reviewed tree, close the issue, and (e2e)
deploy prod and verify; a failure names what merged and the rollback target. Then `office close`.
If the default branch moved after `office start`, run `office land --rebase` first. It re-composes the
accepted work onto the new head and re-runs the run checks and an integration review. A conflict refuses
and prints the steps to compose by hand. Office rewrites only the block above the PR body's
`<!-- office:pr ... -->` line, so put criteria that report data in the PR body below that line. A
check or test-runner timeout while host load exceeds twice the CPU count is UNAVAILABLE, not a
failure. Rerun it with `office resume`.
With task PRs off (local, no GitHub, `--no-prs`), push the integration branch it names, open a PR
with `Closes #<issue>`, and `office close --handoff <pr-url>`. Stop early with
`office close --abandon "<reason>"`; nothing is deleted until `office prune -f`. When the work landed
through a PR Office did not open, `office close --landed-externally <merged-pr-url>` closes it as landed;
if that merge does not contain every accepted revision, ask the user and add `--quote "<words>"`.

## Takeover

When the runtime itself is the bottleneck, suggest `auto-takeover` with the evidence. Only the user starts
it. It composes one integration branch, runs file-disjoint Herdr lanes and exits with `close --landed-externally`.

## Resume

After a restart or compaction: `office resume` (or `office resume <id>` when several runs exist).
It reconstructs pending work from runs.db; never start a new run to continue an old one.

## Diagnostics

`office inspect run|plan|task|gate|evidence|events|route [id]` and `--verbose`/`--json` show the detail default output hides.
`office doctor` checks the install, hooks, pinned runtimes, and known harness defects. `office list` and `office prune` (dry run; `-f` to delete) maintain runs.

The detailed design lives in `docs/v31-implementation.md`; you do not need it to run the lifecycle.
