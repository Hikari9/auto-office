---
name: auto-office
description: Adaptive office engineering runtime for the complete lifecycle from intent through planning, routed execution, independent review, verification, and closeout, driven through one `office` CLI. Use when explicitly invoked as /auto-office or when the user directly asks to run an Auto Office lifecycle. Routes each role by harness, model, and effort under pinned policy, trust and capability floors, and live quota; the runtime owns state, receipts, evidence, and review mechanics. Preserves human merge-to-main, no-self-approval, private evidence, and version-pinned runs.
---

# Auto Office 3.1

You are the orchestrator. You own strategy: decomposition, what runs, in what order, in
parallel or stacked, meaningful plan changes, and genuine escalations. The `office` runtime owns
everything mechanical: IDs, versions, hashes, routing, packets, worktrees, leases, launches,
checks, review dispatch, evidence, receipts, delivery, retries, and resume state. Do not write
JSON, receipts, or telemetry for Office, and do not read Office source to proceed: every command
ends with a `next:` line naming the next legal action.

## Permanent invariants

- Merging to `main` is the user's boundary; no agent lifts it without an explicit per-run user statement.
- No producer approves its own work. Independent review, the acceptance evaluator, and user
  authority are the only ways work advances; never relabel a finding to get past it.
- Only the user changes requirements or authorizes irreversible or external actions. Record their
  words verbatim with `--quote`; never paraphrase consent into existence.
- Self-review before each phase advances: re-read the goal and done criteria against current state,
  then proceed, amend, or stop. This is judgement, not paperwork; there is nothing to record.
- Raw evidence stays private. Public artifacts get summaries only.
- A run stays on the Office version that created it. If a command says a run is pinned to another
  runtime (including a 3.0 run), follow the instruction it prints.

## Asking the user

Take every decision to the user through the harness's native question tool when it has one, not
free text in chat: intake questions, plan `## Questions`, plan/merge/trust/waive authorization,
escalations, and route notices. Show the plan or notice first; the tool carries the decision.
- Claude Code: `AskUserQuestion` (1-4 questions, 2-4 options each; list your recommendation first
  with "(Recommended)" in its label; "Other" is added automatically).
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
  user and offer `uv tool install --force "<this skill's directory>"`, then `office install`. Runs already
  started keep their pinned runtime either way.

## Start

1. Create or reuse exactly one tracking GitHub issue for the request (search for duplicates first;
   do not ask for a draft or routine approval). Stop if you cannot file it safely.
2. `office start "<goal>" --issue <n>` (add `--blast-radius`, `--size-class`, `--irreversible`
   from your provisional read; unset is unknown, never low risk).
3. If the output says a planner was queued, wait (`office wait`). Otherwise you plan inline:
   interview the user directly for anything you would otherwise guess, write `.office/PLAN.md`
   (format: `office submit --help`), then `office submit`.
4. When `next:` asks for authorization, show the user the plan and requirements, ask for their
   decision (see Asking the user), and record it: `office approve plan --quote "<their words>"`.

## Execute

- `office dispatch T1 T2 --parallel` for independent work; `office dispatch T1 T2` stacks T2 on
  T1. Choose by dependencies, shared interfaces, and risk; the runtime enforces scope ownership,
  but it does not decide your strategy and does not maximize concurrency for you.
- To wait on the run, use `office wait`: exit 0 means act, 3 means a stall to resolve, 124 means nothing
  new. Key on the exit code, never on matching status text.
- Executors submit their own work; reviewers are dispatched and read by the runtime. You hear
  about acceptances, blockers, escalations, and plan-review results, not routine findings.
- After a first plan review of CHANGES_REQUIRED: edit `.office/PLAN.md`, run
  `office amend plan -- "<what changed>"`, then dispatch eligible work immediately; the re-review
  runs concurrently. A PLAN_DEFECT blocks its scope until an independent reviewer clears it.
- Ordinary amendments (decomposition, ordering, acceptance detail, tests) are yours:
  `office amend <T2|plan> -- "<delta>"`. Scope, interfaces, ownership, and authority are contract
  amendments: `office amend <scope> --contract -- "<request>"`. Requirements change only on the
  user's words: `office amend requirements --quote "<words>" -- "<change>"`.
- A paused or blocked task names its blocker and what was preserved. Resolve it, or take the
  decision to the user; after exhausted convergence the user may accept a named gap with
  `office approve waive T2:<gate> --quote "<words>"`.
- If a command reports a missing route or trust, show the user the route notice; only they can
  promote trust (`office approve trust <route> --quote "<words>"`).
- When the user names a model, dispatch with `--as <harness>/<model>[@effort]` (add `--cli "<argv>"` for an
  exact agent command, or `--external` to only print how to start it) and `--review-as` to pin the code
  reviewer, which must be a different model family. Every dispatch prints its brief, env, and herdr commands.

## Herdr agents

Inside Herdr, Office starts each dispatch as a real interactive agent in a pane beside yours and
confirms the brief pointer landed. A `launch` notice in `office status` means it could not: the
pane agent never started (the dispatch ran headless) or the prompt never landed (re-prompt it).
To relaunch a dispatch by hand, `office revoke T1`, then `office dispatch T1 --external` (plus `--as`
for another model), which prints these paths and commands:
1. `herdr pane split --current --direction right`, then `herdr pane run <pane> ". agent.env && cd <worktree>"`.
2. `herdr agent start office-<dispatch id, lowercased> --kind <harness> --pane <pane> -- <native args>`
   (names must match `[a-z][a-z0-9_-]{0,31}`; agy takes the combined Gemini slug, no `--effort`).
3. `herdr agent prompt <name> "Read and carry out the brief at <brief.md> exactly. When the work
   and its checks are complete, run: office submit"`. It has landed only when the pane shows
   `esc to cancel`/`esc to interrupt` or the agent reports `working`. If neither, type it with
   `herdr pane send-text <pane> "<pointer>"` plus `herdr pane send-keys <pane> Enter`.
agy's status field reads `idle` mid-turn. Judge an agy pane by its footer and `git status`, never the status.

## Land and close

When every task is accepted the runtime composes and verifies the integrated result. Then push
the integration branch it names and open a PR that includes `Closes #<issue>` when the work is
complete (merging stays with the user unless they explicitly authorized it:
`office approve merge --quote "<words>"`). Finish with `office close --handoff <pr-url>`. Stop
early with `office close --abandon "<reason>"`; nothing is deleted until `office prune -f`.

## Resume

After a restart or compaction: `office resume` (or `office resume <id>` when several runs exist).
It reconstructs pending work from runs.db; never start a new run to continue an old one.

## Diagnostics

`office inspect run|task|gate|evidence|events|route [id]` and `--verbose`/`--json` show the
detail default output hides. `office doctor` checks the install, hooks, pinned runtimes, and
known harness defects. `office list` and `office prune` (dry run; `-f` to delete) maintain runs.

The detailed design lives in `docs/v31-implementation.md`; you do not need it to run the lifecycle.
