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
independent review happens once per ownership/composition lane on the composed result, with a hard
three-round ceiling and every user-owned decision surfaced at once.

## 1. Contract pinning, compatibility, and rollback

| Fact | Detail |
|---|---|
| Where a run records its contract | `gates_json.review_contract`, written at `office start` and never changed afterwards. |
| Run with no recorded contract | Started before #337. It stays on `v3.1` permanently. |
| Default for new runs | `convergence-v1`, from `review.contract` in `config/config.default.yaml`. |
| Rollback | Set `review.contract: v3.1`. It affects runs started afterwards only. A started run is never reinterpreted. |
| Moving old work to the current contract | `office start --from-run <run>` (below). Never an in-place rewrite. |
| Stored verdicts | Never rewritten. A `v3.1` verdict is displayed with its label, for example `PASS (v3.1)`. |

`office start --from-run <run> ["<goal>"]` creates a new run under the current contract that carries
the old run's frozen requirements and puts the old plan's body at the new run's `PLAN.md` path as a
draft. Authorization, plan review and all review records start fresh. The new run records
`landing.derived_from` (source run, its contract, plan version, time) and a `run.derived` event; the
old run gets an appended `run.successor` event and is otherwise untouched. Review the draft, then
`office submit` as for any plan.

`office inspect run` prints the contract on its first line (`review contract <name>`).

## 2. Lifecycle

```text
office start ──> plan ──> plan review ───────────────────────────────┐
                    ^        │ APPROVED: dispatch; findings -> disposition (no re-review)
                    │        │ RECHECK: revise; named tasks (+dependants) held; same reviewer
                    │        │ INTAKE_GAP: ask the user; affected tasks held
                    └────────┘ (3 substantive rounds, then office decide plan ...)
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
                               │ (3 substantive rounds, then office decide <lane> ...)
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
  (for example `src/billing/**, tests/billing/**`; `src/billing/` means the same as `src/billing/**`). Name an
  exact file only at a collision point or a cross-task dependency.
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

Plan review runs when the gear funds it (`plan_review`). `office approve waive plan-review --quote
"<words>"` still waives it.

| Verdict | Effect |
|---|---|
| `APPROVED` | Plan review ends; every eligible executor may fan out. Findings stay tracked until fixed or dispositioned. |
| `RECHECK` | The planner (or the inline orchestrator) revises. Tasks named by blocking findings, and their dependants, are held; a plan-wide finding holds every task. Unaffected work may start. The same reviewer reviews the revision. |
| `INTAKE_GAP` | Ask the user at once. Tasks the gap affects are held. Record the answer (`office amend requirements --quote ...`), revise the plan; a fresh cycle reviews it. |

To submit a revision: a dedicated planner is told `office amend plan --contract -- "<the findings>"`;
inline, edit `PLAN.md`, then `office amend plan --contract -- "<what changed>"`.

After `APPROVED`, a plan revision that moves no hard seam is APPROVED cleanup and is not re-reviewed.
A revision that moves one reopens plan review in a new cycle. For the plan, a hard-seam move is a task
added or removed, or a change to a task's `scope`, `depends`, `interfaces`, `accept`, `lane`,
`converge` or visual applicability, or a new requirements version. Checks, titles, routes and visual
details are not seams.

The orchestrator may veto review of an ordinary amendment: `office amend plan --no-review --reason "doc-only
wording" -- "<delta>"` creates plan p+1 and queues no plan-review gate for it. The reason is required and is
recorded in a `plan.review_skipped` event. Use it when review adds nothing, such as a typo or wording fix, a
reordering that moves no seam, or a note to a worker. A gate already queued or running for an earlier version is
left alone. The veto applies only to ordinary amendments: a contract amendment (`--contract`) or a requirements
amendment is always reviewed, and `--no-review` on either is refused.

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

## 7. Lanes and shared scopes

| Scope | Membership | Id |
|---|---|---|
| Lane | Tasks joined by `depends`, or naming the same `lane:`. The smallest independently landable workstream. | `L-<lane name>` or `L-<first task>` (for example `L-T1`) |
| Shared scope | Lanes that share a `converge:` name, an interface (one provides what another consumes), a `shared:` registry, or changed files. | `S-<lane suffixes>` (for example `S-T1+T3`) |
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

## 8. The repair cycle and round accounting

On `RECHECK`:

1. Office routes the whole batch of open blocking findings at once to every task named in their
   `owner:` fields; those tasks become `changes_required`.
2. The orchestrator runs `office rerun <task> --resume|--fresh` for each owning task, in parallel.
   Findings never relaunch anything on their own.
3. When every repaired task is accepted again, the lane recomposes (a new composed commit).
4. The same reviewer reviews round n+1 when it is still available, with the previous round's open
   findings listed.

A substantive round is a completed review (`COMPLETED`, with a verdict) in the current cycle. Each
RECHECK sequence (plan, each lane or shared scope convergence review, each visual review) is capped at
3 substantive rounds. The cap is not configurable; the old `*_max_rounds` config keys apply only to
`v3.1` runs.

These never spend a round:

- a reviewer or provider failure: Office walks the whole configured fallback chain automatically;
- a malformed or missing reply (`INVALID_RESULT`);
- a capture retry or evidence recovery (`EVIDENCE_BLOCKED`);
- `office resume`, which retries unavailable, attention and evidence-blocked reviews on the same
  composed revision.

## 9. The round cap and operator decisions

After the third `RECHECK` in a cycle nothing runs automatically. The orchestrator asks the user right
away (native question tool), showing `office inspect convergence <scope>` (or `office inspect run` for
the plan): remaining findings, their materiality, the attempts so far, landing risk, and its own
recommendation. It records the answer:

```text
office decide <scope|plan> escalate|continue|waive|stop --quote "<user's words>" [--reason "<why>"]
```

| Choice | Effect |
|---|---|
| `escalate` | A new bounded cycle that excludes the earlier reviewer routes (for the plan, the last reviewer route). For a stronger producer, also `office rerun <task> --fresh --reroute`. |
| `continue` | A new bounded cycle on the same routes. |
| `waive` | Land despite the unmet gate. Needs landing authority (section 11); for a lane it records a user waiver of each unmet gate. For the plan it is `office approve waive plan-review`. |
| `stop` | Pause the lane: its tasks are paused and nothing from it lands. For the plan, held tasks stay held. |

Round exhaustion is never turned into `INTAKE_GAP`. `office decide` is refused for workers and needs
the user's quote.

A lane `INTAKE_GAP` uses the same command: ask the user, record the answer (`office amend
requirements|plan ...`), then `office decide <lane> continue --quote "<user's words>"`.

## 10. APPROVED findings and dispositions

Findings that remain on an `APPROVED` review are non-blocking. Landing and close are blocked until
each has a disposition:

```text
office disposition <scope>:<code>[,<code>] fix|fixed|dismissed|follow-up -- "<note>"
```

`<scope>` is a lane or shared scope id, or `plan`. A note is required except for `fix`.

- `fixed`: already repaired; the note says how.
- `dismissed`: not a problem; the note says why.
- `follow-up`: tracked elsewhere; the note names the issue.
- `fix`: routes an APPROVED cleanup repair. The owning tasks reopen as `changes_required`; run
  `office rerun <task> --resume|--fresh`. Their checks and self-review run and the lane recomposes, but
  it is not re-reviewed, unless the repair changes a task's contract or acceptance version (a hard
  seam), which triggers a new convergence review.

## 11. Waivers and landing authority

Required convergence and visual reviews are hard landing gates. Waiver authority is landing authority
for the run, never a role.

| Actor | Holds waiver authority | Command |
|---|---|---|
| User | Always | `office approve waive L-T1:convergence\|visual --quote "<user's words>" --reason "<why>"` |
| Orchestrator | Only when landing was delegated: intake end state `merge` or `e2e`, or a recorded `office approve merge` | `office approve waive L-T1:convergence --as orchestrator --reason "<why>"` |
| Planner, executor, reviewer dispatch | Never | refused |

A waiver:

- keeps the underlying verdict or status (`RECHECK`, `UNAVAILABLE`, ...); it never rewrites it;
- is bound to the scope, the gate kind and the scope's composed commit. A recomposition voids it unless
  it is renewed;
- appears in the archive receipt under `convergence.waivers`, with the underlying verdict and reason.

Under `convergence-v1`, `office approve waive T2:code_review|visual` and `office approve visual` are
refused: code and visual review are lane gates. Task `checks` waivers are unchanged.

## 12. Degraded fallback review

When every specialist convergence reviewer route failed (`UNAVAILABLE`), the orchestrator may review
the gate itself:

```text
office review <scope>:convergence --report <file>
office review <scope>:visual --report <file> --inspected <every screenshot>
```

The report uses the reviewer reply format. The review is recorded with independence
`degraded-orchestrator` and shown on receipts. For visual, every screenshot of the capture must be
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
office inspect convergence [scope]                lanes, shared scopes, gates, findings, escalations
office inspect run                                shows the run's review contract
office amend plan --contract -- "<what changed>"  submit a plan revision after RECHECK or INTAKE_GAP
office amend plan --no-review --reason "<why>" -- "<delta>"   ordinary amendment, no plan review (never with --contract)
office rerun <task> --resume|--fresh [--reroute]  run a routed repair (RECHECK or disposition fix)
office decide <scope|plan> escalate|continue|waive|stop --quote "<user's words>" [--reason "<why>"]
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
| Plan review | Rolling: after `CHANGES_REQUIRED`, dispatch while the amendment is re-reviewed; a defect blocks its scope until cleared | `RECHECK` holds only named tasks; same reviewer; APPROVED cleanup is not re-reviewed |
| Requirement problems | `PLAN_DEFECT` + `office amend plan --contract --redirect ...` / `office submit --redirect ...` | `INTAKE_GAP` + `office amend requirements --quote ...` |
| Round budget | Gear `*_max_rounds` | 3 substantive rounds per RECHECK sequence, then `office decide` |
| Waivers | `office approve waive T2:<gate> --quote ...` (user) | Lane gates; landing authority; bound to the composed commit |
