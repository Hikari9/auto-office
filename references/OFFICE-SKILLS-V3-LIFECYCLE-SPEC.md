# Auto Office v3 — lifecycle spec

Companion to `OFFICE-SKILLS-V3-SPEC.md`. That document specifies **how a role is
routed**. This one specifies **how a run is driven**: intake, approval, parallel
execution, integration, and the autonomy ceiling.

A **receipt** is an artifact a third party can re-read to confirm a step happened: a state
dir, an approval quote, pasted command output, an exit code. A claim is not a receipt.

An assertion that cannot run is not a weaker gate. It is no gate.

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

Done when: the returned `state_dir` exists for this run.

`check-spoke --state-dir <state_dir> --spoke <name>`

Done when: exit code is 0; enter planning from this receipt.

## 2. Stage 1 — provisional intent, then interactive planner freeze

**This supersedes #47.** The orchestrator no longer conducts the grilled interview or freezes
intent before planning starts. It captures only the provisional intent needed to understand the
request and decide whether planning is required — a hypothesis for the planner, not a frozen
contract. The planner then performs repository reconnaissance, interacts directly with the user
(including through a Herdr pane when available), may reshape any pre-freeze field when evidence
contradicts the provisional framing, and freezes the five fields only at the end of that
interactive discovery (issue-35#decision-1, issue-77 §1-2).

The floor is twelve items, run by the planner. All twelve are covered on every run, delivered as
one or two batched question rounds — or a structured-question tool where the harness has one —
rather than an unbounded back-and-forth:

1. **Outcome** — what is true when this is done, in the user's words.
2. **Done-criteria** — the exact commands, reads or observations that prove it.
   Every requirement names its verification.
3. **Blast radius** — repos, environments, live systems. Production is named
   explicitly or excluded explicitly.
4. **Irreversible steps** — each becomes a `named_actions:` entry, which is what
  later lets the loop perform it autonomously. External sends always stop the loop.
5. **Waves** — which done-criteria can proceed simultaneously, and what must
   serialise behind a shared interface. See section 4.
6. **Interfaces** — signatures, schemas, routes, file boundaries that parallel
   tasks must agree on. Pin before dispatch; a routed executor cannot invent
   them consistently across worktrees.
7. **Constraints** — stack, conventions, domain skills that must load, untouchables.
8. **Speed vs correctness** — which one is being bought. Moves the route.
9. **Executor count** — one repo or several, one slice or several.
10. **User-owned decisions** — anything that would otherwise be guessed.
    Record the decision explicitly; recommend where useful.
11. **Rollback target** — what "undo this" concretely means.
12. **Prior art** — existing work, branches or PRs to preserve and align with.

The five frozen fields (`goal`, `done_criteria`, `blast_radius`,
`named_actions`, `non_goals`) are derived from the answers and frozen by the
planner, at the end of this interactive discovery. The user answers each field before it is frozen.

## 3. Gear is declared by the fit test

The fit test decides the gear from blast radius, reversibility and size class
#45). The kickoff block declares it, giving the user the cheapest place to
overrule it.

The declared gear is visible before execution, so the user owns any override.

## 4. Waves, interfaces and worktrees

The plan groups tasks into **waves**. A wave is a set of tasks with disjoint
write scopes and satisfied dependencies. Every task
declares `Depends on:` and `Touches:`; a wave is the transitive closure of tasks
whose dependencies are already satisfied.

Rules:

- **One mutable holder per write scope.** Tasks in the same wave have disjoint
  files.
- **One worktree per parallel dispatch.** Created via `office_worktree.sh create
  --dispatch-id <id>`, branched from the run's pinned base SHA.
- **The orchestrator commits and merges worker trees.** A worker that runs git
  commands can move a base the orchestrator pinned.
- **A shared interface serialises.** If two tasks must agree on a signature,
  schema or fixture, either the interface is pinned in stage 1 and both proceed,
  or the interface becomes its own task in an earlier wave. Apparent parallelism
  over an unpinned shared contract becomes a serial integration task; packets
  carry the pinned interface.
- **Every dispatch brief carries the pinned contract and `effective_config_hash`.**
  A parallel producer or reviewer reasoning from unpinned config reasons from
  the wrong policy. Observed: a reviewer concluded "executor has no preferred
  seed" from the repo default because the user config tier was invisible to it.
- **A dispatch brief's completion criterion is a command whose output settles
  it, not a target.** "Target under 120 lines" is a wish; "`wc -l` reports
  under 120" is a receipt. Observed: a worker briefed with a wish did the easy
  substitutions, skipped the restructuring, and reported done short of the
  target; rebriefed with the command form, it finished the work.

## 4.1 The dispatch schedule

The plan's wave structure is declared to the user as a schedule, immediately
before the approval prompt and nowhere else. It is the last thing seen before
the single gate, because routing and funding are only overrulable while they are
still free to change.

The schedule names, per task: write scope, routed identity
(`harness@version × model_id × effort`), the dependency that places it in its
wave, and its size class. Bars show size classes (#42); the router produces
classes rather than honest minute estimates.

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

- **Every task has a named route before approval.** Routing happens before
  execution, so every task has a reviewable cost.
- **Every route states why it won** in one clause, from the decisive filter —
  task shape, capability floor, preferred seed, quota, cost or local evidence.
- **The critical path is marked.** It is the only number that predicts wall
  clock, and it is what a reader checks when the schedule looks too wide.
- **Parallel width is stated.** Width above the disjoint-scope limit in section 4
  is a planning defect, visible here before it becomes a merge conflict.
- **Projected quota after the run is stated.** Quota is a planning weight (#58).
  A run that would drain a window the user needs later is a decision they own;
  the router keeps that choice visible.

A drawn schedule demonstrates disjoint waves; failure to draw one is a planning
finding.

## 5. The single approval

One approval authorises everything through closeout. It follows the plan, its
self-review, and any resolved plan review.

Approval is a recorded state transition; the receipt is its state and quote:

```
python3 scripts/office_runtime.py approve-plan --state-dir <d> \
    --approved-by user --quote "<verbatim user words>"
```

Done when: `phase == approved` and `approval.quote` is the verbatim user wording.

Phase order is `intake → planned → approved → executing → reviewed → closed`.
`auto-execution` reads this receipt before dispatch. The `PreToolUse` hook reads it
too, but as defence in depth only. The office does not enforce approval
mechanically on the executor: it states the rule in the brief and trusts the
executor to comply, correcting course when it does not
(`references/why-trust-not-enforcement.md`). What the runtime does hold is cheap
and deterministic — `state-save` cannot write `approved`, a fabricated state
cannot reach `planned`, and a plan-version bump invalidates a prior approval.

`--quote` is an audit record of what was approved. It is not a credential, and it
does not need to be: the orchestrator may approve on the user's behalf toward
subagents. Merging to `main` is the exception that still requires the user's own
explicit statement (§9.1).

## 5.1 Mid-run additions

A follow-up that arrives during an active run is routing input for the orchestrator. The
orchestrator classifies it before acting:

- Fits an already-frozen field → route to an executor as a new execution
  packet in the current or next wave.
- Needs planning, a new interface, or changes the wave structure → route to the planner, increment
  the plan version, invalidate dependent packets.
- Changes one of the five frozen fields → re-freeze and take a new approval. The approval
  authorizes only the approved plan.

The orchestrator announces the route for each addition, the same disclosure as any dispatch
(section 4.1). Inline execution by the orchestrator is legal only when the fix's brief would
exceed the edit itself; this exception stays scoped to that case.

A follow-up whose target write scope is held by a live dispatch waits for that wave, or goes to
the worker already holding the scope (section 4). That worker remains the sole writer.

The orchestrator keeps capacity for mid-run asks and polls dispatches while any dispatch is live
(section 6).

An executor holds a task queue for its write scope; a follow-up routed to an
executor that already has work appends to that queue rather than replacing it, and its prior
tasks, findings and constraints stay binding. Resuming that executor's session is preferred over
spawning a fresh one for the same scope, because the accumulated context is what stops a task
from being dropped. The executor reports one outcome per queued task on completion.

Observed failure this closes: an orchestrator implemented several streamed follow-ups inline
because each looked small, making itself the bottleneck, holding write scopes that belonged to
workers, and dropping the routing and telemetry record that makes a run reviewable.

## 5.2 Ownership assigns dispatch

When the user addresses the orchestrator directly — "you write this", "you handle it" — that
names the office: the orchestrator together with its executors. It assigns ownership and
dispatch; the inline exception in section 5.1 stays scoped to that rule.

Observed failure this closes: the orchestrator read "you write this" as an instruction to edit
files itself; the maintainer corrected it — "by 'you' i always meant you together with your
executors. You own office."

When the orchestrator takes the section 5.1 inline exception, it alone judges whether that edit
needs independent review — self-review or none suffices for a small or documentation-only edit,
and that judgment stands unchallenged. Section 8's independent-review rule governs delegated
production work; this waiver covers only small or documentation-only inline edits.

## 5.3 Amendment kinds, and concurrent families with sticky focus

`requirements_version`, `plan_version`, and `routing_version` amend independently
(issue-35#decision-5). Section 5.1 classifies a follow-up by which of the three it changes:

- **Routing-only** (model/reviewer/parallelism/depth change): the orchestrator updates routing
  state and informs affected executors; a not-yet-started dispatch uses the new route immediately;
  the planner never wakes.
- **Requirements delta that still fits the plan** (architecture, interfaces, dependency order,
  milestones, and done criteria remain valid): the orchestrator versions requirements and sends a
  delta packet to the affected executor(s); the planner never wakes.
- **Plan-contract delta** (architecture, interfaces, dependency ordering, milestone structure, done
  criteria, or a plan assumption invalidated): only affected scopes pause; the planner wakes for
  interactive delta-planning, self-reviews, runs its plan adversary, and emits a new plan version;
  the orchestrator redistributes only what changed.

A running dispatch normally completes its current atomic unit or round before a new route or plan
applies, unless the user explicitly requests immediate replacement (section 5's approval covers the
plan it approved, not an unbounded license to bump every version on every delta — doing so on a
routing-only change is itself a defect).

One orchestrator may supervise multiple independent families concurrently through a durable family
registry holding, per family: repo/issue, phase, the three version counters, ownership, active
dispatches, latest landing, and pending user decisions (issue-35#decision-6). Use **sticky focus**:
exactly one family is the current conversational focus; an unqualified command applies there;
naming another family switches focus and applies there; an explicit global command applies
session-wide; a genuinely ambiguous command mutates nothing and states why rather than guessing.
Families run independently by default — the orchestrator projects quota/resource collisions and
warns the user without delaying unrelated work, and an explicit user routing command always
outranks a projected-collision warning. Full contract in `protocol/families-and-amendments.md`.

## 6. Non-blocking orchestration

The orchestrator stays active while delegated work runs. A blocked orchestrator
serialises a parallel plan and burns the wall-clock term in the reward (#48).

- Dispatch every task in the current wave before waiting on any of them.
- **A dispatch counts as in flight only once it has a receipt**, not a status
  read at dispatch time: an observed transition to working, or output in the
  pane. Harness readiness semantics differ — codex `idle` and agy `idle` do
  not mean the same thing. Observed: a worker reported `idle` at dispatch and
  was prompted, but the prompt never landed; it sat at a bare prompt while the
  orchestrator believed it was working.
- While dispatched work runs, the orchestrator does only work that touches no
  write scope held by a live dispatch: planning later waves, reviewing returned
  output, preparing briefs, reading state.
- Poll while any dispatch is live.
- A wave ends when every dispatch in it has returned or been declared dead by
  two independent liveness signals.

**Completion is event-driven.** The orchestrator
arms a background monitor covering every dispatch at or before the moment it
dispatches a wave, and completion reaches the orchestrator as an event from
that monitor. One monitor stays armed for the life of the run, so a dispatch that finishes
while the orchestrator is mid-conversation with the user still reports;
conversation with the user leaves monitoring armed. It covers every
terminal state: finished, idle, blocked, unknown, and disappeared all have to
fire, because a monitor that only matches the happy path is silent through a
crash. When monitoring is unavailable, the orchestrator re-polls before making
any claim about dispatch state.
On a completion event the orchestrator collects the result, closes the
finished agent's pane, and appends or updates the spawn ledger; pane
reclamation is orchestrator-owned.

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
3. Resolve conflicts at the orchestrator; each worker retains its own tree.
4. Run the plan's validation commands on the integrated result. The receipt is
   validation commands run on the integrated result.
5. Adjudicate contract disagreements explicitly. When a test written against the
   pinned contract disagrees with an implementation, name which one is wrong and
   why. Repo convention outranks an ambiguous contract clause; a contract clause
   outranks an implementation's convenience.
6. After reverting a merge, re-merging the same dispatch branch does not reapply
   it — git treats its changes as already merged and reverted, so the merge
   produces conflict markers instead of the fix. Observed: a bad merge was
   reverted on the integration branch; the worker corrected its branch, the
   orchestrator re-merged it, and the conflict markers landed as a syntax error
   that broke collection for the whole suite. When the dispatch branch wholly
   owns the files in question, take the corrected files directly from it instead
   of merging; otherwise, revert the revert before merging the correction.

Integration is a lifecycle stage with its own validation, because the first
moment N parallel trees have ever existed together is after the merge.

## 8. Review with N producers

- Every **independent approval and review** gate is held by an agent that did
  not produce the work. A producer gating its own delegated output is a
  protocol violation, with one named exception: the section 5.1/5.2 inline
  waiver for small or documentation-only orchestrator edits.
- **Self-verification is a pass, never an approval.** A producer's own
  self-review/self-verification (required by section 4's execution packet and
  performed by every producer, including `auto-execution`) confirms the
  producer's own work meets its own packet — it is not, and cannot stand in
  for, the independent-approval gate above. This is the single normative
  statement of that distinction; other skill files point here rather than
  restating it.
- **Review is local first; a final adversary is integration-boundary-triggered,
  not a default second pass (issue-35#decision-4, issue-77 §4-5,7).** Each
  executor owns its own implementation and review loop and may accept a
  finding and fix it, or reject it with stronger evidence
  (`disposition_owner: executor`). Cheap, reversible, low-risk work may use
  inline self-review instead of an independent adversary
  (`review_mode: labeled-inline`, never represented as
  `independent_adversary`). A final orchestrator-spawned integration adversary
  exists only when two or more executors produce dependent or merging
  landings that must compose across a shared interface — never merely because
  a family has more than one executor.
- **Executor disposition ownership with exceptional upline consultation
  (issue-35#decision-3).** The executor decides review dispositions.
  It escalates to the orchestrator only when it and its reviewer cannot
  responsibly resolve a disagreement; this is exceptional, not a routine
  approval chain. If evidence is genuinely conflicting, the executor emits a
  structured `TRUE_CONFLICT`/`USER_DECISION_REQUIRED` state and the
  orchestrator surfaces it to the user rather than acting as the higher-tier
  technical judge.
- The reviewer's scope is the **integrated** diff.
- Reviewer sessions may run in parallel across independent scopes. One reviewer
  session serialised across N producers is a designed bottleneck.
- Round cap: 5 in `full`, 2 in `express`. A second `CHANGES REQUIRED` on one
  task forces an orchestrator disposition.
- `PLAN DEFECT` and `BRIEF DEFECT` exit without consuming a round.

### 8.1 A receipt for a gate is the reason it gives, not its exit status

A verification matrix that asserts only exit codes cannot tell a gate that blocks correctly from
one that fails closed on everything, because both share the same code on every case tested.
Observed: a change to the approval hook passed a nine-case matrix asserting exit codes (five
expected blocks at exit 2, four expected allows at exit 0) and 103 unit tests, while the hook
could no longer read any run state and returned `unreadable_run_state` for every repository
containing `.office/runs`, including correctly approved runs — it would have halted all work in
every auto-office repo. The allow-cases passed only because those paths had no run state to fail
on. General form: a check whose output has fewer distinct values than the conditions it must
distinguish cannot verify them. Assert the reason a gate gives, not only whether it exited nonzero.

### 8.2 Compaction before adversarial review (issue-77 addendum)

Long-lived roles may compact at semantic phase boundaries, after first serializing the state the
next phase needs. This is conditional, not mandatory for every task:

```text
inline review / tiny task                        -> no compaction needed
adversarial review + executor context still small -> optional
adversarial review + substantial implementation   -> checkpoint + compact before reviewer
very long review/fix loop                          -> compact again at later phase boundaries
```

The checkpoint (`schemas/checkpoint.schema.json`) preserves only what the executor needs to defend
or modify its work: requirements/plan/routing versions, task scope, current commit/diff and changed
files, key implementation decisions and tradeoffs, tests/validation already run, interfaces
touched, deviations from plan, unresolved concerns, and current review round/state. The repository,
diff, tests, and serialized packet remain the source of truth — a role does not pay to retain a
transcript of exploratory reads, failed attempts, or routine tool history to answer a reviewer
later. The reviewer receives a fresh independent packet (plan/requirements, repository/diff,
validation evidence, review scope) rather than the executor's transcript, preserving adversarial
independence.

## 9. Autonomy ceiling

After approval the run proceeds end to end under the approved plan. It:

- bootstraps a plan-only first commit, named branch and draft PR before the
  first task, so the run is resumable from the moment work starts;
- commits and records each wave as it goes green;
- runs verification and review rounds;
- removes the plan file and marks the PR **ready for review** at closeout;
- may merge dispatch branches into its own integration/working branch.

The run stops at a ready PR; `main` remains the human boundary (#35, out of
scope). Merging to `main` on the run's own initiative is prohibited. An explicit
per-run user statement in the approval or conversation may authorize that merge.

It pauses for an **external send** or a **user-owned decision the plan did not
anticipate**. Everything the plan named — production applies included — it executes.

### 9.1 Authority decisions belong to the user

Whether this run may merge to `main` is a decision only the user makes. Roles
within the run carry it out; the user owns the decision. Where the user's explicit
instruction conflicts with an earlier default, gear preset, playbook or prior
instruction, the user's most recent explicit statement governs for that run.

Observed failure: a run was told to merge to `main` and declined, returning a
`BRIEF DEFECT` whose content was about task scope — the scope contradicted a
documented stop decision, and a further blocker had appeared. The scope
observation may have been correct. The error is structural: a defect exit about
the *artifact* was used to settle a question about *authority*.

Two rules follow.

**Reporting and carrying out are separate obligations.** When blockers exist
alongside an authorized merge, state them plainly and concisely, then carry out
the authorized action. Surfacing the evidence is required. `PLAN DEFECT` and
`BRIEF DEFECT` remain available and still pause work and report — a defect exit
is a statement about a plan or a brief; authority remains with the user.

**A defect exit must concern the artifact it names.** Raising a scope objection
in order to decline an authority decision is itself a protocol error worth
naming, because it disguises the real disagreement as a technical one.

The grant covers merging to `main` for the run in which it is given. Other
repositories, environments and destructive actions stay outside the grant, and
later runs require their own authorization.

### 9.2 A blocked tool call is a question, not a verdict

When the harness refuses an action — a permission classifier, a denied tool
call, or a policy guard — surface what was attempted, why it was refused, and
the available options. Ask the user to authorize it; their answer settles it.
Re-attempting the same action in altered wording to get it past a guard is out
of bounds.

## 10. Self-improvement after

Closeout emits proposals; policy changes pass the replay gate (#51). Scope is
unchanged from #52: a dreamt pattern line appended to a reference doc, on the
standing PR, citing the run_id and recorder rows it came from. Floors, weights
and reward definitions stay behind that gate.

A learned pattern that adds an imperative rule to a policy spoke is a policy
change; metadata labels do not change it. It goes through the replay gate.

### 10.1 Where a learned lesson lives

A discipline learned during a run that should change future runs' behavior is persisted in this
spec or a spoke — versioned, reviewable, shipped. Closeout emits the proposal with its run_id and
recorder rows; an imperative policy rule passes through the replay gate.

## 11. Assertable conventions

Each convention has a machine-checkable assertion:

| Convention | Assertion | Known bypass |
|---|---|---|
| Run was actually started | state dir exists for this run | hook state discovery resolved cwd rather than the git root, so a subdirectory command failed open |
| Spoke was loaded | `check-spoke --spoke <name>` exits 0 |  |
| Plan was approved | phase == `approved` and `approval.quote` non-empty | `state-save --phase approved` forged an approval. The quote is an audit field by design, not a credential; the orchestrator may approve for subagents (why-trust-not-enforcement) |
| Dispatch requires approval | `auto-execution` refuses below `approved` |  |
| Mutation requires approval | **not asserted, by decision.** Stated in the brief and trusted to the executor | mechanical enforcement is a non-goal; classifying Bash text cannot decide it and the office does not try (why-trust-not-enforcement) |
| Waves are disjoint | each file belongs to one task in a wave |  |
| Integration was validated | validation commands ran on the merged tree |  |
| Routing slug is real | `check-route-defects` exits 0 |  |
| Catalog row is dispatchable | row has `invocation_model_id` |  |

An empty `Known bypass` cell means nobody attacked that assertion in this review.

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

### 12.1 Closed as a non-goal

Mechanical enforcement of "no mutation below approval" is not a defect this spec
leaves open. It is a non-goal. Five review rounds fixed eleven bypasses in the
`PreToolUse` hook before the fifth returned `PLAN DEFECT`; the conclusion drawn
was not "redesign the gate" but "stop building one". Non-determinism is the cost
of using agents, guardrails belong in the brief, and correction belongs to the
orchestrator. `references/why-trust-not-enforcement.md` holds the reasoning and
the three decisions it settles; issue #94 is closed against it.

Recorded because a spec that claims a gate it does not have is worse than one
that says plainly it decided not to build one.

## 13. Conflict register

| #35 constraint | Contact | Resolution |
|---|---|---|
| Out of scope: parallelism is not this destination | Sections 4, 6, 7 | Explicitly reopened per #35's preamble, limited to waves, worktrees and integration. Milestone-scoped discovery planning stays out. |
| #45: fit test is never a user question | Section 3 | Gear is auto-decided and declared, never asked. Declaration is not a question. |
| #47: planner never talks to the user | Section 2 | **Overturned by issue-35#decision-1.** The planner owns the interview and talks to the user directly; the orchestrator holds only provisional, pre-planning intent. |
| issue-35#decision-4: review is local first, integration-triggered | Section 8, `protocol/verification-review.md` | Local/inline review is the default; a final adversary fires only on dependent/merging multi-executor landings, never by executor count alone. |
| issue-35#decision-5: requirements/plan/routing amend independently | `protocol/families-and-amendments.md` | Three independent version counters; only a plan-contract delta wakes the planner; a running dispatch finishes its atomic unit before a new route applies unless immediate replacement is explicitly requested. |
| issue-35#decision-6: concurrent families, sticky focus | `protocol/families-and-amendments.md` | Durable family registry; unqualified commands target the current focus family; explicit global commands apply session-wide; ambiguous commands mutate nothing. |
| issue-35#decision-7: structured packets, conditional compaction | `protocol/families-and-amendments.md`, §8.2 (this document) | Landing/checkpoint packets are durable truth; compaction is conditional on substantial implementation history, not mandatory for inline/small work. |
| issue-93: approval-hook enforcement | `references/why-trust-not-enforcement.md`, §12.1 | **Void by issue-93's own amendment.** Not part of acceptance; do not reintroduce a mechanical enforcement requirement for either harness. |
| Out of scope: unattended self-merge; human merges `main` | Section 9 | Loop stops at a ready PR. Working-branch merges only. `main` requires an explicit per-run statement from the user. |
| Out of scope: unattended self-merge (redux) | SKILL.md permanent invariants | Reworded to agree with Section 9/9.1: no agent lifts the `main` boundary on its own initiative; only an explicit per-run user statement does. |
| #24: every role is portable; input is a serialized artifact | Sections 2, 4 | Grilled intent and the pinned contract are files, not agent state, so a compacted or transferred role loses nothing. |
| #24/single approval: one approval authorizes one plan | Section 5.1 | A mid-run addition is routed as new input to planner or executor, or triggers re-approval; it never becomes ad hoc orchestrator state, so portability and the single-approval boundary both hold. |
| #23/#51/#52: replay-gated policy, no auto-merge | Section 10 | Unchanged. Section 10 additionally closes the labelling loophole. |
| #54: size is a guardrail against prompt bloat | Whole document | This spec adds stages that change decisions. Prose that changes no decision is a defect in the document. |
| #18/#19 completed under retired map #15 | Section 0 | Imported in minimal form rather than re-decided, preserving the prior reasoning. |
