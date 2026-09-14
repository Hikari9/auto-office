# Auto Office v3 — lifecycle spec

Companion to `OFFICE-SKILLS-V3-SPEC.md`. That document specifies **how a role is
routed**. This one specifies **how a run is driven**: intake, approval, parallel
execution, integration, and the autonomy ceiling.

It exists because the v3 map (issue #35) chartered routing, measurement and
self-improvement, and explicitly deferred execution decomposition. The result
shipped as a routing engine with no lifecycle driver: `grilled intent` appears
once in the routing spec as a transfer format and is never produced by any
procedure, and `parallel`, `wave` and `concurrent` appear nowhere in it at all.

## 0. Relationship to issue #35

Issue #35 constraint 1 retired map #15. Issues #18 and #19 — the milestone and
safe-parallelism decisions — were completed under #15 and orphaned by that
retirement. This spec reopens that scope explicitly, as #35's preamble requires,
and imports the minimum needed to make fan-out real: dependency waves, one
worktree per parallel dispatch, and an integration stage. Full milestone-scoped
discovery planning stays out of scope.

Every other #35 constraint is preserved. Section 13 registers each point of
contact and how it is resolved without conflict.

## 1. Stage 0 — `start`

A run begins with exactly one command:

```
python3 scripts/office_runtime.py start --goal <text> --playbook <playbook> [--gear <gear>]
```

`start` resolves effective config, catalog/adapter/policy snapshot hashes, base
SHA and holder triple; runs the fit test; creates canonical state under
`$XDG_STATE_HOME/auto-office/runs/<run-id>/` with a `.office/runs/<run-id>.ref`
pointer in the target repo; and prints the kickoff block.

Skipping stage 0 is the known failure mode, not an optimisation. Every later
gate keys off the `state_dir` it returns, so an agent that skips it silently
disables the entire gate chain — `check-spoke` cannot run without a state dir,
and a gate that cannot run is a gate that never fails.

Stage 0 is checkable after the fact: no state dir means no run happened.

## 2. Stage 1 — grilled intent

The orchestrator owns the interview. The planner never talks to the user (#47).
`grilled intent` is the output of this stage, not an assumption about the user.

The floor is twelve items. All twelve are covered on every run, delivered as one
or two batched question rounds rather than a conversation:

1. **Outcome** — what is true when this is done, in the user's words.
2. **Done-criteria** — the exact commands, reads or observations that prove it.
   A requirement with no nameable verification is not yet specified.
3. **Blast radius** — repos, environments, live systems. Production named
   explicitly or excluded explicitly. Never inferred.
4. **Irreversible steps** — each becomes a `named_actions:` entry, which is what
   later lets the loop perform it without stopping. External sends are excluded
   by definition and always stop the loop.
5. **Waves** — which done-criteria can proceed simultaneously, and what must
   serialise behind a shared interface. See section 4.
6. **Interfaces** — signatures, schemas, routes, file boundaries that parallel
   tasks must agree on. Pin before dispatch; a routed executor cannot invent
   them consistently across worktrees.
7. **Constraints** — stack, conventions, domain skills that must load, untouchables.
8. **Speed vs correctness** — which one is being bought. Moves the route.
9. **Executor count** — one repo or several, one slice or several.
10. **User-owned decisions** — anything that would otherwise be guessed.
    Recommend, never infer.
11. **Rollback target** — what "undo this" concretely means.
12. **Prior art** — existing work, branches or PRs this must not duplicate or
    contradict.

The five frozen fields (`goal`, `done_criteria`, `blast_radius`,
`named_actions`, `non_goals`) are derived from the answers and frozen by the
orchestrator. Freezing fields the user was never asked about is the defect this
stage exists to prevent.

## 3. Gear is declared, never asked

The fit test decides the gear from blast radius, reversibility and size class
(#45). It is never a thirteenth interview question. It is always **declared** in
the kickoff block, which is the cheapest place for the user to overrule it.

Declaring is not asking. A gear the user never sees is a funding decision made
on their behalf in silence.

## 4. Waves, interfaces and worktrees

The plan groups tasks into **waves**. A wave is a set of tasks with no
write-scope overlap and no unresolved dependency between them. Every task
declares `Depends on:` and `Touches:`; a wave is the transitive closure of tasks
whose dependencies are already satisfied.

Rules:

- **One mutable holder per write scope.** Two tasks in the same wave may not
  touch the same file.
- **One worktree per parallel dispatch.** Created via `office_worktree.sh create
  --dispatch-id <id>`, branched from the run's pinned base SHA. Workers never
  share a tree.
- **Workers do not commit.** The orchestrator commits and merges. A worker that
  runs git commands can move a base the orchestrator pinned.
- **A shared interface serialises.** If two tasks must agree on a signature,
  schema or fixture, either the interface is pinned in stage 1 and both proceed,
  or the interface becomes its own task in an earlier wave. Apparent parallelism
  over an unpinned shared contract becomes a serial integration task.
- **Every dispatch brief carries the pinned contract and `effective_config_hash`.**
  A parallel producer or reviewer reasoning from unpinned config reasons from
  the wrong policy. Observed: a reviewer concluded "executor has no preferred
  seed" from the repo default because the user config tier was invisible to it.

## 4.1 The dispatch schedule

The plan's wave structure is declared to the user as a schedule, immediately
before the approval prompt and nowhere else. It is the last thing seen before
the single gate, because routing and funding are only overrulable while they are
still free to change.

The schedule names, per task: write scope, routed identity
(`harness@version × model_id × effort`), the dependency that places it in its
wave, and its size class. Bars are size classes (#42), never minute estimates —
a point value the router cannot honestly produce is worse than a class it can.

```
gear: full · waves: 2 · parallel width: 3 · critical path: T2 → T6

wave 1  T1 catalog/seed.yaml          codex gpt-5.6-luna xhigh   [██  ] M
        T2 scripts/office_runtime.py  codex gpt-5.6-luna xhigh   [████] L  ← critical
        T3 skills/auto-review/*       claude claude-sonnet-5 hi  [█   ] S
wave 2  T6 skills/auto-planning/*     claude claude-sonnet-5 hi  [██  ] M  (after T2)
        T7 scripts/hooks/*            codex gpt-5.6-luna xhigh   [███ ] L

reviewer: codex gpt-5.6-luna xhigh, fresh per wave, independent of every producer
quota after this run, projected: codex ~60% weekly · claude ~65% weekly
```

Required properties:

- **Every task has a named route before approval.** An unassigned task is an
  unreviewable cost. Routing is not deferred to execution time.
- **Every route states why it won** in one clause, from the decisive filter —
  task shape, capability floor, preferred seed, quota, cost or local evidence.
- **The critical path is marked.** It is the only number that predicts wall
  clock, and it is what a reader checks when the schedule looks too wide.
- **Parallel width is stated.** Width above the disjoint-scope limit in section 4
  is a planning defect, visible here before it becomes a merge conflict.
- **Projected quota after the run is stated.** Quota is weighed, never a gate
  (#58), but a run that would drain a window the user needs later is a decision
  they own, not one the router makes silently.

A schedule that cannot be drawn means the waves are not actually disjoint. That
is a finding about the plan, not a formatting problem.

## 5. The single approval

One approval authorises everything through closeout. It is taken after the plan,
its self-review, and any plan review have resolved.

Approval is a recorded state transition, not a sentence in a transcript:

```
python3 scripts/office_runtime.py approve-plan --state-dir <d> \
    --approved-by user --quote "<verbatim user words>"
```

Phase order is `intake → planned → approved → executing → reviewed → closed`.
`auto-execution` refuses to dispatch below `approved`. A producer running
`approve-plan` without the user's actual words is a protocol violation, and the
recorded approval is what the fail-closed mutation hook reads.

Silence is not approval.

## 5.1 Mid-run additions

A follow-up that arrives during an active run is routing input, not orchestrator work. The
orchestrator classifies it before acting:

- Fits an already-frozen field, no plan change needed → route to an executor as a new execution
  packet in the current or next wave.
- Needs planning, a new interface, or changes the wave structure → route to the planner, increment
  the plan version, invalidate dependent packets.
- Changes one of the five frozen fields → re-freeze and take a new approval. The approval
  authorizes the plan that was approved, not an arbitrarily grown one.

The orchestrator announces the route for each addition, the same disclosure as any dispatch
(section 4.1). Inline execution by the orchestrator is legal only when the fix's brief would
exceed the edit itself — never for volume, never because the chain looks linear, never because
it is faster right now.

A follow-up whose target write scope is held by a live dispatch waits for that wave, or goes to
the worker already holding the scope (section 4). It never becomes a second writer.

The orchestrator stays present to do this: it does not occupy itself with work that would leave
it unresponsive to a mid-run ask, and it polls dispatches rather than blocking on one (section 6).
A blocked or absorbed orchestrator cannot route, which is what this section exists to prevent.

An executor holds a task queue for its write scope, not a single task; a follow-up routed to an
executor that already has work appends to that queue rather than replacing it, and its prior
tasks, findings and constraints stay binding. Resuming that executor's session is preferred over
spawning a fresh one for the same scope, because the accumulated context is what stops a task
from being dropped. The executor reports per-task status on completion — which queued tasks it
finished and which it did not — so three queued tasks yield three outcomes, never one merged
summary. An unreported queued task counts as not done.

Observed failure this closes: an orchestrator implemented several streamed follow-ups inline
because each looked small, making itself the bottleneck, holding write scopes that belonged to
workers, and dropping the routing and telemetry record that makes a run reviewable.

## 6. Non-blocking orchestration

The orchestrator must not idle while delegated work runs. This is a requirement,
not a style preference: a blocked orchestrator serialises a parallel plan back
into a sequential one and burns the wall-clock term in the reward (#48).

- Dispatch every task in the current wave before waiting on any of them.
- While dispatched work runs, the orchestrator does only work that touches no
  write scope held by a live dispatch: planning later waves, reviewing returned
  output, preparing briefs, reading state.
- Poll; never block. A single blocking wait on one worker is a defect when
  another worker has already returned.
- A wave ends when every dispatch in it has returned or been declared dead by
  two independent liveness signals.

**Completion is event-driven, never learned by blocking.** The orchestrator
arms a background monitor covering every dispatch at or before the moment it
dispatches a wave, and completion reaches the orchestrator as an event from
that monitor, not by waiting on one dispatch at a time. The monitor stays
armed for the life of the run, not per wave, so a dispatch that finishes
while the orchestrator is mid-conversation with the user still reports —
conversing with the user must never suspend monitoring. It must cover every
terminal state, not only success: finished, idle, blocked, unknown, and
disappeared all have to fire, because a monitor that only matches the happy
path is silent through a crash, and silence is indistinguishable from still
running. A blocking wait on a single dispatch is a defect whenever another
dispatch is live. If the monitor is lost or was never armed, the orchestrator
re-polls before making any claim about dispatch state — reporting a status it
has not re-read since the last known event is reporting stale state as fact.
On a completion event the orchestrator collects the result, closes the
finished agent's pane, and appends or updates the spawn ledger; pane
reclamation is the orchestrator's job, not the user's to ask for.

Observed failure this closes: an orchestrator hand-rolled blocking waits to
learn when delegated agents finished. It occupied the orchestrator's only
execution thread, so it could not respond to the user and serialised a
parallel wave back into sequence; a user interruption of one wait then cost
the orchestrator its only completion signal, and it went on to report a
worker as still running minutes after that worker had actually finished.
Finished agents' panes accumulated because nothing fired on completion.

## 7. Integration

Between execution and review, the run integrates:

1. Commit each worker's tree on its dispatch branch, authored by the orchestrator.
2. Merge dispatch branches into the run's integration branch in wave order.
3. Resolve conflicts at the orchestrator, never by re-dispatching a worker into
   a tree it does not own.
4. Run the plan's validation commands on the integrated result. A green worker
   tree is not evidence about the merge.
5. Adjudicate contract disagreements explicitly. When a test written against the
   pinned contract disagrees with an implementation, name which one is wrong and
   why. Repo convention outranks an ambiguous contract clause; a contract clause
   outranks an implementation's convenience.

Integration is a lifecycle stage with its own validation, because the first
moment N parallel trees have ever existed together is after the merge.

## 8. Review with N producers

- No producer reviews its own work, including its own merge.
- The reviewer's scope is the **integrated** diff, not a single worker's tree.
- Reviewer sessions may run in parallel across independent scopes. One reviewer
  session serialised across N producers is a designed bottleneck.
- Round cap: 5 in `full`, 2 in `express`. A second `CHANGES REQUIRED` on one
  task forces an orchestrator disposition, not an automatic re-plan.
- `PLAN DEFECT` and `BRIEF DEFECT` exit without consuming a round.
- A green suite is never approval on its own.

## 9. Autonomy ceiling

After approval the run proceeds end to end without further go-aheads. It:

- bootstraps a plan-only first commit, named branch and draft PR before the
  first task, so the run is resumable from the moment work starts;
- commits and records each wave as it goes green;
- runs verification and review rounds;
- removes the plan file and marks the PR **ready for review** at closeout;
- may merge dispatch branches into its own integration/working branch.

It never merges to `main`. Merge to `main` is the permanent human boundary
(#35, out of scope) — unless the user states explicitly, in the approval or in
the conversation, that this run may merge to `main` autonomously. Absent that
sentence, the run stops at a ready PR.

It stops for exactly two other things: an **external send**, and a **user-owned
decision the plan did not anticipate**. Everything the plan named — production
applies included — it executes.

## 10. Self-improvement after

Closeout emits proposals, never policy. Scope is unchanged from #52: a dreamt
pattern line appended to a reference doc, on the standing PR, citing the run_id
and recorder rows it came from. Floors, weights and reward definitions stay
behind the replay gate (#51). Auto-merge on green evals stays permanently ruled
out (#23).

A learned pattern that adds an imperative rule to a policy spoke is a policy
change, not a pattern, regardless of how its metadata is labelled. It goes
through the replay gate.

## 11. Assertable conventions

Conventions that are only prose are conventions agents skip under load. Each of
these is machine-checkable, and the check is the convention:

| Convention | Assertion |
|---|---|
| Run was actually started | state dir exists for this run |
| Spoke was loaded | `check-spoke --spoke <name>` exits 0 |
| Plan was approved by the user | phase == `approved` and `approval.quote` non-empty |
| No dispatch before approval | `auto-execution` refuses below `approved` |
| No mutation before approval | fail-closed `PreToolUse` hook on Edit/Write |
| Waves are disjoint | no file appears in two tasks of one wave |
| Integration was validated | validation commands ran on the merged tree |
| Routing slug is real | `check-route-defects` exits 0 |
| Catalog row is dispatchable | row has `invocation_model_id` |

An assertion that cannot run is not a weaker gate. It is no gate.

## 12. Known defects this spec closes

- `start` had no runnable form; `new-run` needed nine arguments sourced from
  commands the skill never sequenced, so skipping the runtime was the cheapest
  correct-looking path.
- The five frozen fields were frozen from orchestrator invention because no
  stage produced them from the user.
- Gear was defined in `protocol/lifecycle.md` with no selection procedure and no
  declaration point.
- The lifecycle had no representation of concurrent dispatch, no integration
  stage, and no wave concept for the planner's `dependencies` field to feed.
- Review had no round cap and one serialised reviewer session.
- `office_worktree.sh --dispatch-id` was built for per-dispatch isolation and
  referenced only in a tooling bullet list.

## 13. Conflict register

| #35 constraint | Contact | Resolution |
|---|---|---|
| Out of scope: parallelism is not this destination | Sections 4, 6, 7 | Explicitly reopened per #35's preamble, limited to waves, worktrees and integration. Milestone-scoped discovery planning stays out. |
| #45: fit test is never a user question | Section 3 | Gear is auto-decided and declared, never asked. Declaration is not a question. |
| #47: planner never talks to the user | Section 2 | The interview is orchestrator-owned. The planner receives grilled intent as a serialized artifact. |
| Out of scope: unattended self-merge; human merges `main` | Section 9 | Loop stops at a ready PR. Working-branch merges only. `main` requires an explicit per-run statement from the user. |
| #24: every role is portable; input is a serialized artifact | Sections 2, 4 | Grilled intent and the pinned contract are files, not agent state, so a compacted or transferred role loses nothing. |
| #24/single approval: one approval authorizes one plan | Section 5.1 | A mid-run addition is routed as new input to planner or executor, or triggers re-approval; it never becomes ad hoc orchestrator state, so portability and the single-approval boundary both hold. |
| #23/#51/#52: replay-gated policy, no auto-merge | Section 10 | Unchanged. Section 10 additionally closes the labelling loophole. |
| #54: size is a guardrail against prompt bloat | Whole document | This spec adds stages that change decisions. Prose that changes no decision is a defect in the document. |
| #18/#19 completed under retired map #15 | Section 0 | Imported in minimal form rather than re-decided, preserving the prior reasoning. |
