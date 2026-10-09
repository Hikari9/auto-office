---
name: auto-office
description: Adaptive office engineering runtime for the complete lifecycle from intent through planning, routed execution, independent review, verification, and closeout, driven through one `office` CLI. Use when explicitly invoked as /auto-office or when the user directly asks to run an Auto Office lifecycle. Routes each role by harness, model, and effort under pinned policy, trust and capability floors, and live quota; the runtime owns state, receipts, evidence, and review mechanics. Preserves human merge-to-main unless the user chose merge or end-to-end at intake, no-self-approval, private evidence, and version-pinned runs.
---

# Auto Office

`MANIFESTO.md` is the product philosophy and outranks this file; the runtime outranks both (`docs/orchestrator-reference.md` has the order).
You are the orchestrator. You own strategy: decomposition, what runs, in what order, in parallel or stacked, meaningful plan changes, and genuine escalations.
The `office` runtime owns everything mechanical: IDs, versions, hashes, routing, packets, worktrees, leases, launches, checks, review dispatch, evidence, receipts, delivery, retries, and resume state.
Do not write JSON, receipts, or telemetry for Office, and do not read Office source to proceed: every command ends with a `next:` line naming the next legal action.

## Permanent invariants

- Merging to `main` is the user's boundary; no agent lifts it without an explicit per-run user statement.
- No producer approves its own work. Independent review (a different producer and reviewer session), the acceptance evaluator, and user authority are the only ways work advances; a waiver is never an approval; never relabel a finding to get past it.
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
- **Not found:** stop and ask the user to approve installing it; never install silently. Then `uv tool install "<this skill's directory>"`, `office install`, `office doctor`.
- **Release differs from this directory's `VERSION`, or `office doctor` prints `install: STALE`:** tell the user and offer `uv tool install --force --reinstall "<this skill's directory>"`, then `office install`. Runs already started keep their pinned runtime.

At lifecycle intake, run `office update --check`; if it reports an update, offer it in the next native intake question round. A failed or offline check does not block intake. Detail: `docs/orchestrator-reference.md`.

## Start

1. Check for an Auto Office update as described above, then create or reuse exactly one tracking GitHub issue for the request (search for duplicates first;
   do not ask for a draft or routine approval). Stop if you cannot file it safely.
2. Ask how far to go after the task PRs: stop and ask after (`ask`), preview deploy only (`preview`),
   merge only (`merge`), or merge + prod deploy (`e2e`, which is merge and deploy authority). For a
   deploy, show `office land --detect`'s proposed commands and have the user confirm them.
3. `office start "<goal>" --issue <n> --end-state <answer>` (`--deploy-preview|prod|verify "<cmd>"`
   as confirmed; `--blast-radius local|repo|production|production-data`, `--size-class S|M|L|XL`,
   `--irreversible` from your provisional read;
   unset is unknown, never low risk: an unknown run keeps independent code review under every gear until the plan
   classifies it; see "Risk floor and the lightweight path" below). Never ask about benchmark refreshes; that is the
   `auto-update-benchmarks` skill, run only when the user explicitly calls it.
4. If the output says a planner was queued, wait (`office wait`). Otherwise you plan inline:
   interview the user directly for anything you would otherwise guess, write `.office/plans/<run>/PLAN.md` (the path `office start` prints)
   (format: `office submit --help`), then `office submit`.
5. `office submit` prints the plan diagram (waves, stacking, each task's route slate, checkpoints;
   `office inspect plan` reprints it). When `next:` asks for authorization, show the requirements
   and the diagram verbatim, ask (see Asking the user), then `office approve plan --quote "<words>"`.
   After an amendment show only the printed delta.

## Execute

- Dispatch as the approved diagram shows: a wave's roots with `office dispatch T1 T2 --parallel`, dependents stacked
  (`office dispatch T1 T3`). Dispatch runs each task's declared primary route or names the fallback it took and why; when every
  planned route is out, it stops: `office dispatch <task> --reroute`. The effective route (harness, model, effort) is recorded at
  dispatch; `office status` lists it and `office inspect route <task>` adds every change (old/new, reason, actor, time). To change a
  pending or running task's route: `office amend route <task> --as <harness>/<model>[@effort] --quote "<words>"`. Office follows the
  recorded route on redispatch and rerun and never swaps it silently.
- When the user names a model, dispatch with `--as <harness>/<model>[@effort]` (`--cli "<argv>"` for an exact command, `--external` to
  only print how to start it). `--review-as` pins the reviewer of the task (v3.1) or lane (convergence-v1). A reviewer is always a
  fresh session, never the executor's, though it may share the executor's model.
- A dispatch failure is a recovery checkpoint, not permission to abandon the run: read the launch notice, `office inspect task <T> --verbose`
  and any pane Office names, then follow the printed `next:` (re-prompt, `office resume`, `office rerun <task> --resume|--fresh`,
  `office dispatch <task> --reroute`). Never approve hook trust, other directories, credentials, irreversible actions or user authority for
  the user. Close a pane only by the id Office names, never your own (`$HERDR_PANE_ID`). Detail: `docs/orchestrator-reference.md`.
- `office wait`: exit 0 means act, 3 a stall, 5 an agent asked a question, 124 nothing new. Key on the exit code, never on status text.
  Exit 5 prints a `question:` line. Decide planning, scope, ordering and test detail yourself (amend the contract first if the answer
  changes it), then `office answer <task|dispatch> <n>` or `-- "<text>"`. Take it to the user (native question tool) only when it hints at
  requirements, authority, or an irreversible or external action; never answer those on your own.
- `office status` makes one `herdr agent list` call and prints a `blocker:` line for a live pane dispatch herdr reports `blocked` with no
  recorded question. Run `office wait` to record it; status itself records nothing.
- Executors push their task branch as they work and submit; the runtime pushes the accepted revision to the task's draft PR (stacked on its
  parent's), posts review results, and marks it ready on acceptance. Reviewers are dispatched and read by the runtime, only from their reply
  files, never pane text. A bad reply re-prompts the same reviewer (no round spent); after three it needs you.
- Never run commands inside a task worktree yourself; reproduce a check in a scratch copy. A stalled executor, quota wall, host-load timeout
  or headless worker is handled as `docs/orchestrator-reference.md` says, via `office wait`, `office prompt` and the printed `next:`.

- Executors simplify (behavior-preserving, in SCOPE), self-review on four lenses at a depth Office sets from the run's
  gear and risk (`inline`, `single`, or `deep`; an unset blast radius is never `inline`), record every finding in
  `OFFICE_SELF_REVIEW.md` (severity as found; low findings are fixed but do not trigger a re-review; a medium or high fix
  that changes behavior gets one fix-diff re-review and names its test plus `mutation=failed`; 3-round cap;
  `contract-conflict` stops with the ACCEPT line quoted; submit consumes the file), then `office preflight`, which
  refuses a missing, stale, or open ledger. Each ends with one `TASK=... SUBMIT=... NEXT=...` line (saved in `pane-final.txt`); act on its `NEXT=`. A worker
  refused as lease-lost, superseded-dispatch, or task-paused is done: never prompt it to retry.
- Before submitting a plan inline, run the same four lenses over it and put what they surface in tasks' `accept:` criteria. Plan seams, not
  internals (`scope:` is an ownership envelope; `shared:`, `lane:`, `converge:`, `accept_needs:`, `integration_risk: high`): see
  `docs/orchestrator-reference.md`.
- Ordinary amendments (decomposition, ordering, acceptance detail, tests) are yours: `office amend <T2|plan> -- "<delta>"`. Plan review
  reviews the initial plan only: once it closes (APPROVED, its round cap, or a waiver) no amendment is reviewed again; you own them. While
  it is still open a revision is its next round; to skip that for a trivial one, `office amend plan --no-review --reason "<why>" -- "<delta>"`.
- A note is prose only. To change what is enforced, edit the task's structured contract: `office amend T1 --add-check "<cmd>"` (also
  `--add-accept`, `--drop-check`, `--drop-accept`, `--set depends=T2`). It versions the contract, runs the new check at the gate (an accepted
  task reruns it and reopens only on failure), records old and effective contract plus rationale (`office inspect amendments`), and never
  queues plan review. Authority words in an edit are refused. Scope, interfaces, ownership and authority are contract amendments
  (`--set scope=...` / `--set interfaces=...` need `--contract`; a changed scope or interface reopens the task and its dependants):
  `office amend <scope> --contract -- "<request>"`. Requirements change only on the user's words:
  `office amend requirements --quote "<words>" -- "<change>"`.
- A paused or blocked task names its blocker and what was preserved: resolve it, or take the decision to the user. A missing route or trust
  shows a route notice; only the user promotes trust (`office approve trust <route> --quote "<words>"`).

## Review

New runs pin the `convergence-v1` review contract; `docs/review-convergence.md` is the reference.
- Plan, convergence and visual reviews answer APPROVED (advance now; findings get a disposition, not another review), RECHECK (producers
  repair blocking findings; the same reviewer rechecks) or INTAKE_GAP (ask the user the missing decision now). UNAVAILABLE and other runtime
  failures are not verdicts: `office resume`, no round spent. Independent means a different producer and reviewer session.
- Plan RECHECK holds only the tasks its blocking findings name: revise `PLAN.md`, then `office amend plan --contract -- "<what changed>"`;
  dispatch unaffected work meanwhile. At the plan round cap (3, or `office start --plan-review-rounds N`) plan review closes unapproved and
  you own the findings: fix each in the plan or accept it, then `office disposition plan:<code> fixed|dismissed|follow-up -- "<rationale>"`.
  Do not ask the user just because the rounds ran out. Another plan review only on the user's ask: `office review plan --quote "<words>" [--rounds N]`.
- A task is accepted on its own checks. When a lane's tasks (joined by `depends` or `lane:`) are all accepted, Office composes them and runs
  one convergence review, plus a visual review for user-visible acceptance. Lanes sharing a boundary, or a run with risk or a shared
  outcome the runtime names, get one integrated review (#422); `office inspect convergence` states why it was required or skipped.
- Findings never relaunch anything on their own. A RECHECK routes every blocking finding to its owning tasks at once: `office rerun <task>
  --resume|--fresh` for each, in parallel. Before landing, give each APPROVED finding a disposition: `office disposition <scope>:<code>
  fix|fixed|dismissed|follow-up -- "<note>"` (`fix` reopens the owner without re-review).
- At the lane round cap (3 by default; `office start --review-rounds N` or `review.max_rounds`, pinned per run) nothing runs and review does
  not stay blocked. Choose one: waive and accept the residual risk with a substantive reason, `office waive <scope> --reason "<why the open
  findings are acceptable>"`, or escalate to the user with the findings, attempts, risk and your recommendation, then `office decide <scope>
  escalate|continue|waive|stop --quote "<words>"`. A waiver never becomes APPROVED (the verdict stays RECHECK and the receipt records the
  reason, findings, composed commit and your session) and is not landing authority: landing still needs the user's authorization.
- Outside the cap, only landing authority waives a required review: the user (`office approve waive L-T1:convergence|visual --quote "<words>"
  --reason "<why>"`), or you (`--as orchestrator --reason "<why>"`) only when the end state is merge/e2e or a merge is authorized. If your
  host blocks `--as orchestrator` as self-approval (Claude Code auto mode), never work around it: ask the user.
- When no specialist reviewer returns a verdict (every route UNAVAILABLE, or the last reply INVALID_RESULT), the orchestrator may review on its
  behalf: `office review L-T1:convergence --report <file>` (visual: `--inspected <every screenshot>`). Specialists come first. It counts as
  independent only when your session did not produce the work; a session that did is refused. Runs started before #423 record it as degraded and non-independent.
- **v3.1 runs** (started before #337; `office inspect run` names the contract) keep PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT,
  per-task review and `--redirect`: follow their `next:` lines and `docs/v31-rolling-review-gates.md`. Runs started before #423 and #418 keep
  their older cap, waiver and plan-review behavior until `office upgrade`.

### Risk floor and the lightweight path

- Every run records a risk classification: `low` (explicit `local`/`repo` blast radius, nothing high), `elevated` (irreversible, production
  blast radius, size L/XL) or `unknown` (nothing declared). Unknown is never low. No gear or mode drops independent code review below the floor
  `unknown` and `elevated` imply.
- The plan classifies it in Requirements (`blast_radius: local|repo|production|production-data`, optional `irreversible: yes`,
  `size_class: S|M|L|XL`). It may classify an unknown run or raise any run, never lower what the user declared. Stored once, restored on resume.
- Trivial low-risk work may declare `lightweight: <why trivial and low risk>`. The runtime refuses it for unknown or elevated risk, gear `full`,
  and after plan authorization. It drops only review the gear tunes (`light`/`quick`/`direct` independent review, `express` plan review).
  Scope, `checks:`, self-review, the evidence receipt and human landing authority all stay. Runs from before this keep their gates.

## Herdr agents

Inside Herdr, each dispatch is a real interactive agent in a pane beside yours, closed once its result is accepted (`office dismiss` closes kept
panes). Message a live worker only with `office prompt <T2|dispatch> -- "<message>"`, never `herdr pane run` or `pane send-text` on an agent
pane. A `launch` notice in `office status` means the agent never started or the prompt never landed; a worker with no pane runs headless and
queues prompts. Pane, headless and manual-relaunch detail: `docs/orchestrator-reference.md`.

## Land and close

When every task is accepted and every lane converged, the runtime composes and verifies the integrated result. `office land` then follows the
end state. `ask` lists the task PRs: ask the user and record the choice with `office land --merge|--preview|--e2e --quote "<words>"`, or stop
with `office close --handoff <pr-url>`. `preview` deploys and verifies the integrated result. `merge`/`e2e` merge the PRs bottom-up after required
checks, confirm the default branch matches the reviewed tree, close the issue, and (e2e) deploy prod and verify; a failure names what merged and
the rollback target. Then `office close`. If the default branch moved, run `office land --rebase` first. `office close --abandon "<reason>"` stops
early; `--landed-externally <merged-pr-url>` closes work that landed through a PR Office did not open. Rebase, `--no-prs` and PR-body detail:
`docs/orchestrator-reference.md`.

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