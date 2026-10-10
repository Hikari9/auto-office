# Review convergence (`convergence-v1`, #337)

This is the operator and implementer reference for the review contract new Auto Office runs use.
It replaces the review policy of `docs/v31-rolling-review-gates.md` and the review sections of
`docs/v31-implementation.md` for new runs. Those documents still govern runs pinned to the `v3.1`
review contract (see [Compatibility](#1-contract-pinning-compatibility-and-rollback)).

Code: `src/office/contract.py` (constants, pinning), `src/office/convergence.py` (lanes, shared scopes,
convergence and visual review, waivers, dispositions, operator decisions, receipts),
`src/office/plans.py` (plan review), `src/office/briefs.py` (`VERDICT_RULES`,
`CONVERGENCE_REVIEW_FORMAT`, `CONVERGENCE_PLAN_REVIEW_FORMAT`, planner and executor guidance).

The design in one sentence: executors build and self-check inside an ownership envelope, and
independent review happens once per ownership/composition lane on the composed result, with a round
ceiling (three by default) and every user-owned decision surfaced at once.

## 1. Contract pinning, compatibility, and rollback

| Fact | Detail |
|---|---|
| Where a run records its contract | `gates_json.review_contract`, written at `office start` and never changed afterwards. |
| Run with no recorded contract | Started before #337. It stays on `v3.1` permanently. |
| Default for new runs | `convergence-v1`, from `review.contract` in `config/config.default.yaml`. |
| Rollback | Set `review.contract: v3.1`. It affects runs started afterwards only. A started run is never reinterpreted. |
| Moving old work to the current contract | `office start --from-run <run>` (below). Never an in-place rewrite. |
| Stored verdicts | Never rewritten. A `v3.1` verdict is displayed with its label, for example `PASS (v3.1)`. |
| Initial-only plan review (#418) | Ships in Auto Office 3.4. A run on the 3.3 line keeps 3.3's plan-review behavior until `office upgrade` moves it. The upgrade is refused while its plan review waits on an `office decide plan` choice, was stopped by one, or re-reviews after APPROVED; finish or waive that on 3.3 first. Review history is carried unchanged. |

`office start --from-run <run> ["<goal>"]` creates a new run under the current contract that carries
the old run's frozen requirements and puts the old plan's body at the new run's `PLAN.md` path as a
draft. Authorization, plan review and all review records start fresh. The new run records
`landing.derived_from` (source run, its contract, plan version, time) and a `run.derived` event; the
old run gets an appended `run.successor` event and is otherwise untouched. Review the draft, then
`office submit` as for any plan.

`office inspect run` prints the contract on its first line (`review contract <name>`).

## 2. Lifecycle

```text
office start ──> plan ──> initial plan review ───────────────────────┐
                    ^        │ APPROVED: review closes; dispatch; findings -> disposition
                    │        │ RECHECK: revise; named tasks (+dependants) held; same reviewer
                    │        │ INTAKE_GAP: ask the user; affected tasks held
                    └────────┘ (3 substantive rounds, then review closes and the
                               orchestrator owns the findings; never reopened by an amendment)
                                                                        │
          approve plan ──> dispatch tasks (parallel / stacked) <────────┘
                               │
         executor: build in envelope -> checks -> four-lens self-review (<=3 rounds) -> preflight -> submit
                               │
                    task accepted on deterministic checks (no per-task independent review)
                               │
         all tasks of a lane accepted ──> compose lane onto run base (one composed commit)
                               │
              ┌────────────────┴─────────────────┐
     convergence review (if gear funds     visual review (if a task's acceptance
     independent code review)              is user-visible), same commit
              └────────────────┬─────────────────┘
                               │ APPROVED: lane converged; findings -> disposition
                               │ RECHECK: all blocking findings -> owning tasks at once
                               │          -> office rerun each (parallel) -> recompose -> same reviewer
                               │ INTAKE_GAP: ask the user -> office decide <lane> continue
                               │ (round cap, then office waive <lane> --reason ... or office decide <lane> ...)
                               │
         lanes that share converge:/interface/shared:/files ──> shared scope S-... reviewed once
                               │
         every scope converged (APPROVED or waived) ──> integration: compose all + run-level checks
                               │                       (no separate integration review)
         undispositioned APPROVED findings block ──> office land ──> office close
```

## 3. Verdicts, severity, and status

Plan review, convergence review and visual review all answer with one of three verdicts.

| Verdict | Meaning | What happens |
|---|---|---|
| `APPROVED` | No blocking finding. | The work advances now. Remaining findings are tracked as non-blocking and must be fixed or dispositioned before landing, without another independent review. |
| `RECHECK` | At least one finding blocks progression. | The producers repair; the same reviewer (when still available) reviews the next round. |
| `INTAKE_GAP` | The right answer depends on a missing or conflicting user-owned decision that requirements, repository evidence and the existing contract cannot settle. | The orchestrator asks the user at once; what the gap affects waits. |

Every reply also gives `NEXT <recommended next action>`. It is guidance (fix inline before the next
irreversible action, fix concurrently, follow up), never a verdict.

Severity (`high | medium | low`) says how serious a finding is. Blocking (`blocking | non-blocking`)
says whether progression must wait for another independent review. They are decided separately: a
high finding can be non-blocking and a low one blocking.

Runtime or evidence status is separate from both. Only `COMPLETED` carries a verdict.

| Status | Meaning | Spends a round |
|---|---|---|
| `COMPLETED` | The reviewer answered with a readable verdict. | Yes, if it is a substantive round (section 8). |
| `UNAVAILABLE` | No reviewer route could review (tooling, quota, provider failure) after walking the whole fallback chain. | No |
| `EVIDENCE_BLOCKED` | The evidence the review needs could not be produced (for example a visual capture). | No |
| `INVALID_RESULT` | The reviewer answered but left no readable reply. | No |

Visual capture comparability (`COMPARABLE | INVALID_COMPARISON | NOT_APPLICABLE | CAPTURE_BLOCKED`)
stays evidence state, as before.

### Hard seams

A finding whose repair would move a hard seam is never `APPROVED` cleanup. The reviewer marks it
`seam: <seam>` and answers `RECHECK`, or `INTAKE_GAP` when the right change needs the user.

Hard seams: `requirements`, `authority`, `ownership`, `dependency`, `interface`, `acceptance`.

A hard seam shapes a reviewer's verdict. It does not decide when plan review runs: a plan revision
that moves one after plan review closed is owned by the planner/orchestrator and is not reviewed
again (#418).

`PLAN_DEFECT` and `BRIEF_DEFECT` are not verdicts in this contract. They survive only as optional
`root-cause:` metadata on a finding (useful classes: `requirement-contradiction`,
`false-contract-assumption`, `unsafe-or-unauthorized-action`, `double-scope-ownership`).

## 4. Reviewer reply format

Reviewers write only these lines to the reply file their prompt names. Office reads the file, never
the pane.

```text
VERDICT: APPROVED | RECHECK | INTAKE_GAP
FINDING <id> | high|medium|low | blocking|non-blocking | <where> | <what is wrong> | <fix> | owner: T1,T2 [| seam: <seam>] [| root-cause: <class>]
RESOLVED <id>
RETRACT <id> | <evidence it was wrong>          (convergence and visual review)
NEXT <recommended next action>
DECISION <the user decision>   AFFECTS <scope>   WHY <why evidence cannot settle it>   (INTAKE_GAP only)
```

- Plan findings use `P` ids and name affected tasks in `<where>` so unaffected work can start; they
  carry no `owner:` field. Convergence and visual findings use `F` ids and must name `owner:` so
  repairs route to the right tasks.
- A reviewer reports every blocking finding it can identify in one pass. Drip-feeding findings across
  rounds is the failure this contract is built to prevent.
- In a later round the brief lists the open findings of the previous round. The reviewer confirms each
  (repeats the `FINDING` line), marks it `RESOLVED`, or `RETRACT`s it with evidence. A completed
  review supersedes earlier blocking findings it did not restate.
- A tooling, quota or evidence failure is not a verdict. A reviewer that cannot review says so in
  `NEXT` and stops.

## 5. Planning and plan review

### Planner contract: contract the seams, not the internals

Nail down details the builder cannot safely rediscover independently; leave the rest to builder
judgment.

- The planner owns architecture, task boundaries, ownership envelopes, dependencies, shared
  interfaces, and acceptance and test seams.
- `scope:` is an ownership envelope: the module or domain directories a task owns plus their tests
  (for example `src/billing/**, tests/billing/**`). Name an exact file only at a collision point or a
  cross-task dependency.
- `shared:` lists append-only registries several tasks must extend (auth or gate manifests, endpoint or
  grant lists, exhaustive policy maps, shared mocks). They are merged at compose and never count as
  double ownership.
- `lane: <name>` (optional) puts tasks that must land together in one lane. Without it, lanes come
  from `depends`.
- `converge: <name>` (optional) marks lanes that share a requirement, interface or larger outcome, so
  their shared boundary gets its own review.
- `visual:` declares user-visible acceptance. The runtime also flags acceptance that reads as
  user-visible without a `visual:` block and still gives it a gate.

### Plan review

Plan review reviews the initial execution plan. Reviewer-requested corrections may iterate within
that initial review. Once the initial review closes, later amendments are owned by the
planner/orchestrator and do not automatically reopen plan review. A user may explicitly request
another review.

It runs when the gear funds it (`plan_review`). `office approve waive plan-review --quote "<words>"`
still waives it.

| Verdict | Effect |
|---|---|
| `APPROVED` | Plan review closes; every eligible executor may fan out. Findings stay tracked until fixed or dispositioned, with no re-review. |
| `RECHECK` | The planner (or the inline orchestrator) revises. Tasks named by blocking findings, and their dependants, are held; a plan-wide finding holds every task. Unaffected work may start. The same reviewer reviews the revision. |
| `INTAKE_GAP` | Ask the user at once. Tasks the gap affects are held. Record the answer (`office amend requirements --quote ...`), revise the plan; a fresh round budget reviews it. |

To submit a revision: a dedicated planner is told `office amend plan --contract -- "<the findings>"`;
inline, edit `PLAN.md`, then `office amend plan --contract -- "<what changed>"`.

**The cycle.** The initial review is one cycle. It is open from the first submit until it closes,
and only an open cycle queues a reviewer. Every revision submitted while it is open (a RECHECK fix, an
INTAKE_GAP answer, an ordinary or contract amendment) is reviewed as its next round. The cycle closes
on `APPROVED`, at its round cap, or on a waiver. A closed cycle stays closed: no amendment of any kind
(ordinary, contract, requirements), task boundary, `office resume`, reconcile pass, or late reviewer
result reopens it. A round still queued when the cycle closes is cancelled; if its reviewer answers
later, the result is kept as audit evidence (`plan.late_result`) and applied to nothing.

**The round cap.** 3 substantive rounds by default. The user may choose another cap (1 to 10) for a
run at intake: `office start ... --plan-review-rounds N`. It is pinned in the run's gate policy
(`gates_json.plan_review_max_rounds`, with `plan_review_rounds_by: user`) and a `plan.review_rounds`
event, and survives `office resume`. Repository or user config cannot set it.

**At the cap.** A `RECHECK` on the last round closes the cycle unapproved
(`budget_exhausted_orchestrator_owned`). The RECHECK verdict is stored as given; no APPROVED receipt is
written. No further reviewer runs and the user is not asked merely because the rounds ran out. The
orchestrator owns the outstanding findings (listed in `plan_review.outstanding` and `office status`):
it fixes each in the plan (not re-reviewed) or accepts it, and records why with
`office disposition plan:<P-id> fixed|dismissed|follow-up -- "<rationale>"`. Tasks the findings name
stay held until their findings are dispositioned. A fix that changes requirements or authority still
needs the user, as always. `office decide plan` no longer exists.

**After the cycle closes.** The planner/orchestrator owns every later revision: scope, ownership,
dependencies, interfaces, acceptance, lanes, `converge`, visual applicability, tasks added or removed,
and the plan that follows an authorized requirements change. Those amendments still version the plan
and tasks, deliver deltas, pause and restack dependants, and re-run checks as before; they just get no
plan reviewer. Authority is unchanged: requirements change only with the user's quote and a new
authorization, and an ordinary amendment that adds an external, irreversible or destructive action is
refused as a contract-level change.

**Another review, on request.** Only the user reopens plan review:
`office review plan --quote "<user's words>" [--rounds N]` (default 3). It opens a new bounded cycle on
the current plan, recorded as user-requested (an authorization row `plan:review` with the quote, a
`plan.review_requested` event, `lifecycle: user-requested`). RECHECK iterates inside it like the
initial cycle, and it closes the same way. `plan_review.history` lists every closed cycle with its
lifecycle, outcome, rounds and cap.

`office amend plan --no-review --reason "<why>" -- "<delta>"` stays for an ordinary amendment made
while the cycle is open: it creates plan p+1 and queues no plan-review round for it, recorded in a
`plan.review_skipped` event. It is refused for contract and requirements amendments. After the cycle
closes it is never needed.

Plan findings are dispositioned with `office disposition plan:<P-id> fixed|dismissed|follow-up --
"<note>"`. `fix` is refused for plan findings: the planner fixes them in the plan, then you record
`fixed`.

## 6. Execution

- Executors own exact files, helpers, internal APIs, refactors, technique and extra tests inside their
  envelope. The seams (declared interfaces, dependencies, `accept:`, requirements, authority, other
  tasks' scope) are not theirs: when doing the work right needs a seam to move, they ask (`QUESTIONS`)
  instead of working around it.
- Each task runs its deterministic checks and a four-lens self-review (security, edge cases,
  platform and build, test strength) with a 3-round cap. `office preflight` enforces the ledger and
  the cap; this mechanism is unchanged from 3.2.
- A task is accepted on its own deterministic checks. There is no routine per-task independent code
  review and no per-task visual gate; both moved to the lane.
- **Pre-existing check failures.** When a task check fails, Office runs the same command on a checkout
  of the revision's `base_commit`. The failure is pre-existing only when it fails the same way there:
  the same nonzero exit status and the same failures. It is never pre-existing when the task's output
  names a file the task changed, or when either run timed out (Office's timeout or a test runner's).
  A pre-existing-only failure is not a blocking repair finding: the checks gate is `APPROVED`, no repair
  round is delivered, and the failure is recorded as a nonblocking pre-existing note. The base output is
  kept as evidence on the gate and a `gate.preexisting` event is emitted. Lane review still runs. When a
  task also introduces a failure, only that failure blocks and loops rounds, exactly as before.

## 7. Lanes and shared scopes

| Scope | Membership | Id |
|---|---|---|
| Lane | Tasks joined by `depends`, or naming the same `lane:`. The smallest independently landable workstream. | `L-<lane name>` or `L-<first task>` (for example `L-T1`) |
| Shared scope | Lanes that share a `converge:` name, an interface (one provides what another consumes), a `shared:` registry, a cross-lane acceptance dependency (`accept_needs:`), high integration risk, or changed files. | `S-<lane suffixes>` (for example `S-T1+T3`) |
| Rebase scope | Created by `office land --rebase`: the whole run re-composed onto the new base. | `S-rebase` |

Plan waves are never review boundaries, and having several planners adds no extra review.

When every task of a lane is accepted, Office composes their accepted revisions onto the run base into
one composed commit and runs, on that same commit and in parallel:

- one independent convergence review, when the gear funds independent code review
  (`independent_code_review` in the gear preset, recorded on the run as `code_review`);
- one specialist visual review, when a task in the lane has user-visible acceptance. Shared scopes
  get no visual review.

A shared scope is queued after all its member lanes converged, and gets one more convergence review of
the shared composition. Integration starts only after every lane and shared scope converged
(`APPROVED`, waived, or not required).

`office inspect convergence [scope]` shows lanes, shared scopes, gates, findings and escalations.

### Integrated review (#422)

The lane review is the normal unit. An integrated review is one shared-scope review bound to the exact
composed commit of the lanes involved. It is required when lanes share:

- an interface (`interfaces: provides X` in one lane, `consumes X` in another),
- a cross-lane acceptance dependency (`accept_needs: T2` on a task whose acceptance needs another lane's result),
- a shared outcome (`converge:`) or a `shared:` registry,
- or high integration risk: the run's recorded high risk (`risk.high`: production blast radius, L/XL size,
  irreversible) or a task declaring `integration_risk: high`. Office reads these markers as they are
  and adds no new classification. Absence of a marker is never risk.

Lanes that merely ran in the same wave, with none of these, add no review. When a prior independent
APPROVED review already judged the same composed tree over at least the same tasks, the shared scope
converges as `not_required` with the covering scope named and no second review runs. A single lane is
always covered by its own lane review.

Office records why the review was required or skipped: a `convergence.integrated_review` event (shown in
`office status`), `landing.integrated_review`, the first line of `office inspect convergence`, and the
`integrated_review` section of the landing and archive receipts. The review uses the same RECHECK
routing, round cap and orchestrator cap waiver (#423) as any shared scope. Only runs on the current
contract get it. Runs pinned to `v3.1` are unchanged.

## 8. The repair cycle and round accounting

On `RECHECK`:

1. Office routes the whole batch of open blocking findings at once to every task named in their
   `owner:` fields; those tasks become `changes_required`.
2. The orchestrator runs `office rerun <task> --resume|--fresh` for each owning task, in parallel.
   Findings never relaunch anything on their own.
3. When every repaired task is accepted again, the lane recomposes (a new composed commit).
4. The same reviewer reviews round n+1 when it is still available, with the previous round's open
   findings listed. When its harness session cannot be resumed (no session id was recorded, no
   resume form, no herdr session), the round runs in a fresh session on the same route, and the
   orchestrator is told first: the RECHECK line names it, and a `review.resume_fallback` notice
   ("Reviewer D… cannot be resumed: …; the recheck continues in a fresh session on its route …")
   is recorded before the fresh reviewer starts (#406).

A substantive round is a completed review (`COMPLETED`, with a verdict) in the current cycle. Each
RECHECK sequence (plan, each lane or shared scope convergence review, each visual review) is capped at
3 substantive rounds by default. The lane, shared-scope and visual cap is configurable (#423): `review.max_rounds`
(1 to 10) in config, or `office start ... --review-rounds N`. It is pinned in the run's gate policy
(`gates_json.convergence_max_rounds`) when the run starts, so it survives `office resume` and recovery; round
accounting itself lives in the run database. A run started before #423 has no pinned cap and keeps 3. The old
`*_max_rounds` config keys apply only to `v3.1` runs. The plan review's cap has its own user override (section 5,
Plan review).

These never spend a round:

- a reviewer or provider failure: Office walks the whole configured fallback chain automatically;
- a malformed or missing reply (`INVALID_RESULT`);
- a capture retry or evidence recovery (`EVIDENCE_BLOCKED`);
- `office resume`, which retries unavailable, attention and evidence-blocked reviews on the same
  composed revision.

## 9. The round cap and operator decisions

This section is about lanes and shared scopes. Plan review has no operator decision: at its cap the
orchestrator owns the findings (section 5, Plan review).

After the capped `RECHECK` in a cycle (the third by default) nothing runs automatically and review does not
stay blocked. The scope is `escalated` and the `next:` line offers two outcomes.

**Waive (orchestrator, #423).** The orchestrator accepts the residual risk:

```text
office waive <scope> --reason "<why the open findings are acceptable>"
```

The reason must be substantive (at least four words, twenty characters, not a placeholder such as `ok` or
`ship it`) or the command is refused. It works only on a scope that spent its cap, never from a dispatched
agent, and only on runs that pinned a cap at start. The reviewer verdict stays `RECHECK`; it is never rewritten to
`APPROVED`. Each waived gate gets a receipt bound to the scope, the gate kind and the composed tree that records
the reason, the underlying verdict, the unresolved findings (code, level, location, owners), the round and cap, and
the orchestrator session that exercised it. A recomposition to a different tree voids it; one with the same tree
keeps it (section 9, Tree carry-over). The receipt appears in the archive receipt
(`convergence.waivers`) and in the lines `office land` prints. It is not landing authority and needs none
(section 11): landing still needs the user's authorization, from intake (`end_state: merge|e2e`) or later.

**Escalate to the user.** The orchestrator asks the user right away (native question tool), showing
`office inspect convergence <scope>`: remaining findings, their materiality, the attempts so far, landing risk,
and its own recommendation. It records the answer:

```text
office decide <scope> escalate|continue|waive|stop|review --quote "<user's words>" [--reason "<why>"]
```

| Choice | Effect |
|---|---|
| `escalate` | A new bounded cycle that excludes the earlier reviewer routes. For a stronger producer, also `office rerun <task> --fresh --reroute`. |
| `continue` | A new bounded cycle on the same routes. The open blocking findings are routed to their owners, which reopen as `changes_required`, and the command prints which tasks it reopens. |
| `review` | A new bounded cycle that reviews the composed lane again and reopens no accepted producer. The open findings go to the reviewer, which resolves or restates them. |
| `waive` | Land despite the unmet gate. Needs landing authority (section 11); it records a user waiver of each unmet gate. Every kind is checked before any is waived, so a gate that cannot be waived stops the whole command. |
| `stop` | Pause the lane: its tasks are paused and nothing from it lands. |

Round exhaustion is never turned into `INTAKE_GAP`. `office decide` is refused for workers and needs
the user's quote.

**Dismissing a blocking finding.** When the user judges a blocking finding a reviewer defect, the
orchestrator records their words instead of sending the finding to a producer with nothing to change:

```text
office disposition <scope>:<code>[,<code>] dismissed --quote "<user's words>"
```

Without `--quote` a blocking finding is refused and the message names `--quote`; `fixed`, `follow-up` and `fix`
never apply to a blocking finding. With it, the quote is recorded as the user's authority (the note defaults to it),
the finding stops holding work (state `nonblocking`, disposition `dismissed` by the user), and any repair the finding had
routed is released. Each unmet gate left with no open finding is waived on the user's authority, so its
`RECHECK` verdict stands, and the scope settles once no unmet gate remains. A gate that still has open findings
keeps the scope waiting. The archive receipt lists the dismissal (`convergence.dispositions`, with `blocking`,
`disposition_by` and `authority_quote`) and the waiver.

**Tree carry-over.** A scope that is settled (approved, waived, or not required) or still reviewing, and
recomposes to a tree equal to its previous composed tree, starts no new convergence or visual cycle. A
byte-identical resubmit is the usual cause: the compose merge commit is new, its tree is not. The earlier gates,
verdicts, waivers and finding dispositions stay in force for the new commit (`convergence.carried`). Waivers record
the composed tree and match on it; a waiver recorded before trees were stored still binds its commit only, so it
does not carry. A scope in `RECHECK` is reviewed again after its repairs, and `decide` always starts a new cycle.

A lane `INTAKE_GAP` uses the same command: ask the user, record the answer (`office amend
requirements|plan ...`), then `office decide <lane> continue --quote "<user's words>"`.

## 10. APPROVED findings and dispositions

Findings that remain on an `APPROVED` review are non-blocking. Landing and close are blocked until
each has a disposition. The same command records the orchestrator's disposition of a blocking plan
finding left when plan review closed at its round cap; that releases the tasks it held:

```text
office disposition <scope>:<code>[,<code>] fix|fixed|dismissed|follow-up -- "<note>"
```

`<scope>` is a lane or shared scope id, or `plan`. A note is required except for `fix`. A blocking lane finding is
dismissed only with the user's `--quote` (section 9, Dismissing a blocking finding).

- `fixed`: already repaired; the note says how.
- `dismissed`: not a problem; the note says why.
- `follow-up`: tracked elsewhere; the note names the issue.
- `fix`: routes an APPROVED cleanup repair. The owning tasks reopen as `changes_required`; run
  `office rerun <task> --resume|--fresh`. Their checks and self-review run and the lane recomposes, but
  it is not re-reviewed, unless the repair changes a task's contract or acceptance version (a hard
  seam), which triggers a new convergence review.

## 11. Waivers and landing authority

Required convergence and visual reviews are hard landing gates. Outside the round cap, waiver authority is
landing authority for the run, never a role. The round-cap waiver of section 9 is the one exception: the
orchestrator exercises it without landing authority, and it never grants any.

| Actor | Holds waiver authority | Command |
|---|---|---|
| User | Always | `office approve waive L-T1:convergence\|visual --quote "<user's words>" --reason "<why>"` |
| Orchestrator | Only when landing was delegated: intake end state `merge` or `e2e`, or a recorded `office approve merge` | `office approve waive L-T1:convergence --as orchestrator --reason "<why>"` |
| Orchestrator, at the round cap only (#423) | Needs no landing authority; is not landing authority | `office waive L-T1 --reason "<why>"` |
| Planner, executor, reviewer dispatch | Never | refused |

A waiver:

- keeps the underlying verdict or status (`RECHECK`, `UNAVAILABLE`, ...); it never rewrites it;
- is bound to the scope, the gate kind and the scope's composed tree. A recomposition to a different tree voids it
  unless it is renewed; one with the same tree keeps it. The receipt also names the commit it was granted on;
- appears in the archive receipt under `convergence.waivers`, with the underlying verdict, reason, scope, commit,
  acting session and (for a cap waiver) the unresolved findings, and in the `WAIVED ...` lines of `office land`.

Under `convergence-v1`, `office approve waive T2:code_review|visual` and `office approve visual` are
refused: code and visual review are lane gates. Task `checks` waivers are unchanged.

## 12. Degraded fallback review

When every specialist convergence reviewer route failed (`UNAVAILABLE`), the orchestrator may review
the gate itself:

```text
office review <scope>:convergence --report <file>
office review <scope>:visual --report <file> --inspected <every screenshot>
```

Specialist reviewers come first; the fallback is available only after every route failed or returned no
readable reply. The report uses the reviewer reply format. Independence means a different agent or session from
the producer (#423). It fails closed: the review is recorded as `independent-orchestrator` only when this session's
harness session id (`OFFICE_SESSION`) is known and every producer dispatch of the scope has a recorded, different
session id. The same id is refused (`self-review-prohibited`). An unknown identity or an unrecorded producer
session records `degraded-orchestrator`. The ids come from the environment and are not proof against a hostile
session. Runs started before #423 record the
fallback as `degraded-orchestrator`. Either is shown on receipts. For visual, every screenshot of the capture must be
listed in `--inspected`; otherwise no visual verdict is recorded and the gate stays blocked for a
capable reviewer (`office resume`) or a waiver.

Producers can never self-approve, and a dispatched agent cannot use `office review`. Acting as the
fallback reviewer grants no waiver or landing authority.

## 13. Integration, rebase, landing, and receipts

- Integration composes everything and runs the run-level checks. There is no separate integration
  review under `convergence-v1`: the lane and shared-scope reviews already covered the boundaries.
- `office land --rebase` re-composes onto the new base. Lanes stay converged; the `S-rebase` shared
  scope is reviewed once, then integration runs again.
- Landing and close are blocked by an unconverged scope or an undispositioned APPROVED finding.
- The archive receipt (`archive-receipt.json`) names `review_contract`. For convergence runs it has a
  `convergence` section: scopes, reviews (verdict, runtime status, evidence status, independence,
  route, commit), waivers (target, actor, quote, reason, underlying verdict, authority basis) and
  finding dispositions.

## 14. CLI quick reference

```text
office start "<goal>" [--from-run <run>]          new run; --from-run carries an old run's requirements + plan draft
office start "<goal>" --plan-review-rounds N      the user's cap for the initial plan review (default 3)
office inspect convergence [scope]                lanes, shared scopes, gates, findings, escalations
office inspect run                                shows the run's review contract
office amend plan --contract -- "<what changed>"  submit a plan revision after RECHECK or INTAKE_GAP
office amend plan --no-review --reason "<why>" -- "<delta>"   while plan review is open: ordinary amendment, not reviewed
office rerun <task> --resume|--fresh [--reroute]  run a routed repair (RECHECK or disposition fix)
office start "<goal>" --review-rounds N          the lane/shared/visual round cap (default 3; config review.max_rounds)
office waive <scope> --reason "<why>"             at the round cap: the orchestrator accepts residual risk (not landing authority)
office decide <scope> escalate|continue|waive|stop --quote "<user's words>" [--reason "<why>"]   (lanes only)
office review plan --quote "<user's words>" [--rounds N]   the user's request for another plan review
office disposition <scope>:<code>[,<code>] fix|fixed|dismissed|follow-up -- "<note>"
office approve waive <scope>:convergence|visual --quote "<user's words>" --reason "<why>"
office approve waive <scope>:convergence|visual --as orchestrator --reason "<why>"   (delegated landing only)
office approve waive plan-review --quote "<user's words>"
office review <scope>:convergence|visual --report <file> [--inspected <screenshots>]   (degraded fallback)
office resume                                     retry unavailable/evidence-blocked reviews; no round spent
```

## 15. The v3.1 contract, for comparison

Runs pinned to `v3.1` keep these semantics; Office never converts them.

| Topic | v3.1 | convergence-v1 |
|---|---|---|
| Verdicts | `PASS`, `CHANGES_REQUIRED`, `PLAN_DEFECT`, `BRIEF_DEFECT`, `UNAVAILABLE` | `APPROVED`, `RECHECK`, `INTAKE_GAP`; `UNAVAILABLE` is a status |
| Independent code review | Per task, on each reviewed revision | Per lane and shared scope, on the composed commit |
| Visual review | Per task | Per lane |
| Integration review | At a real boundary (dependent output, shared file or interface) | None; covered by shared scopes |
| Plan review | Rolling: after `CHANGES_REQUIRED`, dispatch while the amendment is re-reviewed; a defect blocks its scope until cleared | Initial plan only: `RECHECK` holds only named tasks; same reviewer; once closed, no amendment is re-reviewed |
| Requirement problems | `PLAN_DEFECT` + `office amend plan --contract --redirect ...` / `office submit --redirect ...` | `INTAKE_GAP` + `office amend requirements --quote ...` |
| Round budget | Gear `*_max_rounds` | 3 substantive rounds per RECHECK sequence (lane cap configurable), then `office waive` or `office decide` (lanes) or orchestrator-owned findings (plan; cap user-overridable) |
| Waivers | `office approve waive T2:<gate> --quote ...` (user) | Lane gates; landing authority; bound to the composed commit |
