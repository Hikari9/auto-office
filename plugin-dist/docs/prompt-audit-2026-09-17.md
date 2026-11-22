# Prompt audit — office-skills skill surface

Run: 2026-09-17 · Guide: `claude-api` `shared/prompt-audit.md`

## Stated assumptions (Step 0)

- **Scope.** Model-facing markdown in the main checkout: `SKILL.md`, `skills/*/SKILL.md`,
  `skills/*/references/*.md`, `protocol/*.md` (23 files, ~6.5k words). Scripts excluded per
  request. `references/OFFICE-SKILLS-V3-SPEC.md`, `docs/*`, and `.worktrees/*` excluded — the
  first two are normative design docs read on demand rather than the per-run prompt surface, and
  worktrees are copies of the same files.
- **Target model.** Current-generation frontier models across the harnesses this runtime routes
  (Claude Opus 5 / Sonnet 5 via `claude-cli`, `gpt-5.6-*` via `codex-cli`, Gemini via
  `agy-cli`/`hermes-cli`). No single vendor pin exists in the repo, so findings are restricted to
  patterns that are dated for *every* current generation.
- **Non-Anthropic markers present** (`codex`, `gpt-oss`, Gemini, Google AI). This is a
  multi-harness orchestration repo, not an Anthropic SDK project; no provider switch is proposed.

## Summary

| Group | Findings |
|---|---|
| 1 — Dated prompt text | 4 (F1, F2, F5, F10) |
| 2 — Brittle skill files | 3 (F6, F12, F13) |
| 3 — Tool/skill descriptions | 1 (F8) |
| 4 — Request config / architecture | 0 |

**Clean areas, stated explicitly.** Pressure language (Group 1a) is essentially absent: zero
`CRITICAL|IMPORTANT|MUST|NEVER|ALWAYS` in caps across all 23 files, no `try to`/`if possible`
hedges on real requirements, no trait claims. Group 1b scaffolds are absent: no "think step by
step", no `<scratchpad>`, no prefill, no `budget_tokens`/`temperature`, no forced-tool-use
patterns. Group 1f is absent: no interim-update cadences, no word ceilings (`auto-loop` quotes
"under 120 lines" only as an example of the anti-pattern it forbids). Group 4 is clean.

**The three highest-impact findings.** F2 (issue-decision archaeology — 14 sites citing
`issue-35#decision-N` / `issue-47` / `issue-77 §N`, unresolvable by a routed executor with no
repo history and written as diffs against superseded rules) is the largest and most mechanical.
F13 (the `check-spoke`/`mark-spoke` conditional repeated seven times verbatim in the top-level
`SKILL.md`, the one file loaded on every run) is the largest token win. F10 (hardcoded seed model
names in prose — "Opus Medium", "Astra Low", "Luna XHigh" — sitting beside a config mechanism
that already owns them) is the one with a live correctness risk: the prose and
`roles.<role>.preferred_seed` can disagree, and only the config is read at route time.

---

## Findings

### F1 — Incident narratives embedded in rules · **High** · `rewrite`

**Group 2**, "History narratives: past tense, incident IDs, PR numbers" + **Group 1d** fossils.

| Location | Evidence (abridged) |
|---|---|
| `SKILL.md:68` | "This was missed for a full multi-PR run despite `HERDR_ENV=1` being set the entire time (herdr on `PATH`, a real workspace/tab/pane present) purely because dispatch defaulted to whatever tool was already loaded, without ever checking." |
| `SKILL.md:105` | "a run has already reached dispatch with two spokes marked and neither loaded … that is the exact shape the incident took." |
| `skills/auto-execution/SKILL.md:14` | "This is not a hypothetical: a reviewer dispatched read-only was asked to write its findings to a path, reviewed correctly for sixteen minutes at high effort, then spent further turns trying a text editor, a terminal, an IDE and a browser as write fallbacks before reporting it could not deliver." |
| `skills/auto-closeout/SKILL.md:23` | "is exactly the condition that let a run reach dispatch with `auto-routing` and `auto-execution` marked and neither loaded." |
| `skills/codex-cli/SKILL.md:33-36` | "Observed: a reviewer dispatched as `nohup env -i HOME=… PATH=… TERM=dumb codex exec --yolo …` … Two sibling runs' reviewers were visible in `herdr agent list` at that moment; this one was not." |
| `skills/auto-review/references/review-tree-state.md:1-7` | Whole file is a narrative restating one sentence of `auto-review/SKILL.md`. |

**Why obsolete.** A rule's authority is the behavior it prescribes; the retelling is archaeology
the model cannot act on. Each of these is the recency trap made permanent — one session's stumble
written up at paragraph length. The *reason* is keepable context (keep-list #1); the retelling is
not.

**Action.** Keep one clause of causal reason per rule, drop the narrative. Example rewrite for
`auto-execution/SKILL.md:14`: "A read-only dispatch cannot deliver its result as a file. State the
delivery channel in the packet (`output.delivery`) so `validate-packet` rejects the contradiction
before the reasoning budget is spent." (49 words → 33, rule intact.) Same treatment at each site.
`review-tree-state.md` folds into its one-line rule and the file is deleted along with the
`auto-review/SKILL.md:24` pointer.

---

### F2 — Issue-decision archaeology and migration-relative phrasing · **High** · `remove`

**Group 1d**, "Migration-relative phrasing: 'X now works differently', 'no longer'" + **Group 2**
history narratives.

Sites: `skills/auto-intake/SKILL.md:3,8`; `skills/auto-planning/SKILL.md:8`;
`protocol/roles-and-authority.md:4,7,10,13`; `protocol/lifecycle.md:5`;
`protocol/families-and-amendments.md:3,25,41`; `protocol/verification-review.md:9`.

Worst instances:

- `skills/auto-intake/SKILL.md:3` — "This spoke is planner-owned and requires talking to the user
  directly; it supersedes the older orchestrator-owned, planner-silent interview
  (issue-35#decision-1, overturning issue-47)."
- `protocol/roles-and-authority.md:7` — "This supersedes the older rule that the planner never
  talks to the user and owns only implementation (issue-35#decision-1, overturning issue-47)."

**Why obsolete.** The text is a diff against a prompt version the model never saw. Stating the
superseded rule alongside the live one introduces a phantom alternative the model must first read
and then discard — and a routed executor in a separate process cannot resolve `issue-35#decision-1`
to anything. The current rule is stated correctly on both sides of the "supersedes"; only the
diff framing and the citation are removable.

**Action.** `remove` the "supersedes / overturning" clauses and the bare `(issue-N#decision-M)` /
`(issue-77 §N)` citations; keep every rule statement unchanged. Where provenance genuinely needs
to survive for maintainers, it belongs in `docs/`, not in a file that ships to every run.

---

### F5 — Observation detail beyond the rule in CLI primitives · **Medium** · `rewrite`

**Group 2**, "Volatile specifics … API claims with no verification date" (partially satisfied) +
**Group 1d** fossils.

| Location | Evidence |
|---|---|
| `skills/agy-cli/SKILL.md:17` | "Verified 2026-09-12 on agy 1.2.2: `--model claude-sonnet-4-6` (the slug) is accepted without error and silently falls back to the account default (observed: Gemini 3.7 Flash Low)" |
| `skills/claude-cli/SKILL.md:18` | "Measured 2026-09-15: `modelSettings["claude-sonnet-5"].effortLevel` was `medium` while the top-level was `high`, so `claude --model claude-sonnet-5` ran a routed `high` executor at `medium` for all 105 turns." |
| `skills/agy-cli/SKILL.md:37` | "Observed again 2026-09-14: the same modal blocked a worker for several minutes while status cycled `idle` then `working`" |

**Why obsolete — partially.** The dated-verification convention here is *correct* and the guide
prescribes it; these are not removals. What is dated is the incidental detail: the specific
fallback model observed, the turn count, the second restatement of an already-stated observation.
That detail pins the text to one session and rots independently of the rule.

**Action.** `rewrite` — keep "Verified <date> on <version>: <behavior>", drop the parenthetical
observations and the duplicate second sighting. `agy-cli:37` loses "Observed again 2026-09-14: …
so an orchestrator polling status alone concludes the worker is fine" (the preceding sentence
already says the harness reports `done` rather than `blocked`).

---

### F6 — Routing filter order stated three times · **Medium** · `rewrite`

**Group 2**, "duplicated info across SKILL.md and reference files … information lives in exactly
one place."

- `SKILL.md:54` — nine-step filter order, prose numbered list, plus the derived-input paragraph.
- `skills/auto-routing/SKILL.md:12,14` — same nine steps as an arrow chain, plus the same
  derived-input paragraph.
- `protocol/routing.md:5,7-13` — the normative version.

The three copies currently **agree**, which under keep-list #8 makes this a refactoring
preference rather than a defect — except that `SKILL.md:8` declares itself "the compact control
plane" whose rule is "Load only the protocol/reference needed for the current lifecycle step."
Restating the full filter order there is the file contradicting its own contract, and it is the
copy most likely to drift because it is edited for reasons unrelated to routing.

**Action.** `rewrite` `SKILL.md:54` down to the route identity, the pointer, and the one
invariant the orchestrator must hold without loading the spoke (derived inputs are never
caller-supplied). Leave `auto-routing` and `protocol/routing.md` as they are — a spoke restating
its own protocol is working redundancy.

---

### F8 — Behavioral rules smuggled into skill frontmatter descriptions · **Medium** · `remove`

**Group 3**, "behavior-smuggling … A description is a contract about functionality, not a channel
for conversational instructions."

| Location | Evidence |
|---|---|
| `skills/auto-review/SKILL.md:3` | "Never reuse the producer session as its own independent reviewer." |
| `skills/auto-self-improve/SKILL.md:3` | "Never activate unmerged policy into the current run or merge its own proposal to main." |
| `skills/auto-planning/SKILL.md:3` | "Do not use as a separate lifecycle or to change frozen requirements silently after freeze." |
| `skills/{agy,claude,codex,hermes}-cli/SKILL.md:3` | "Do not treat this primitive as … proof of adapter promotion." |

**Why obsolete.** Descriptions ride in every request's skill listing whether or not the skill is
invoked, so behavioral text there is paid for by every run of every unrelated task. Each of these
rules is already stated in the body of its own skill, where it is read at the moment it applies
(`auto-review/SKILL.md:8`, `auto-self-improve/SKILL.md:8,23`, `auto-planning/SKILL.md:8`,
`protocol/adapters.md:8`).

**Deliberate split applied.** The *negative-trigger* halves stay — "Do not treat this primitive as
a separate lifecycle office", "Do not use to ask about gear, which is declared, not interviewed"
are routing text preventing mis-invocation, and Group 3 explicitly protects those. Only the
clauses that describe what to do *once inside* the skill are removed.

**Action.** `remove` the behavioral clause from each description; keep the trigger and
anti-trigger clauses verbatim. Net ≈ 60 words off every request.

---

### F10 — Seed model names pinned in prose beside the config that owns them · **Medium** · `rewrite`

**Group 2**, "pinned model names silently degrade after the next release" + **Group 1d** fossils.

| Location | Evidence |
|---|---|
| `skills/auto-planning/SKILL.md:19` | "Seed planner preference is Opus Medium then Astra Low; local evidence may supersede." |
| `protocol/roles-and-authority.md:7` | "Seed preference: Opus Medium, then Astra Low, then the normal router" |
| `protocol/roles-and-authority.md:10` | "Reviewer seed may prefer Luna XHigh when local evidence supports it." |

**Why obsolete.** `skills/auto-routing/SKILL.md:48` and `protocol/routing.md:19` already describe
the real mechanism: `roles.<role>.preferred_seed` in resolved config drives the advisory anchor,
and resolution runs `prompt/CLI > repo > user > plugin default`. A user-level
`~/.config/auto-office/config.yaml` can therefore set a preferred seed that these three prose
lines contradict, and only the config is read at route time. This is the one finding where the
duplicate copies can *disagree*, which is exactly the condition keep-list #8 carves out.

**Action.** `rewrite` all three to name the mechanism, not the model:
"Planner seed preference comes from `roles.planner.preferred_seed` in resolved config (see
`protocol/routing.md` § advisory anchor); local evidence may supersede it." Same for the reviewer
line. Defaults stay where they already live, in `config/config.default.yaml`.

---

### F12 — Lifecycle order stated twice in two notations · **Medium** · `rewrite`

`SKILL.md:46` (20 numbered steps) and `protocol/lifecycle.md:3` (the same order as an arrow
chain). **Group 2** duplication. They currently agree; two hand-maintained orderings of a rule
whose whole value is that it is fixed is a drift surface with no upside.

**Action.** `rewrite` `SKILL.md:46` to a pointer at `protocol/lifecycle.md`, keeping only the
invariant already stated at `SKILL.md:13` (never reorder; gears may omit optional stages).

---

### F13 — `check-spoke`/`mark-spoke` conditional repeated seven times · **Medium** · `rewrite`

**Group 1c**, "repetition as reinforcement … near-duplicate sentences across sections."

`SKILL.md:38, 40, 50, 66, 84, 85, 97` each carry a full restatement of the same conditional:
"run `check-spoke --state-dir <state_dir> --spoke X`; if it exits nonzero, load
`skills/X/SKILL.md` via the Skill tool and `mark-spoke --spoke X` before …".

`SKILL.md:103-105` then states the protocol properly and generically, including the
acknowledgement that the earlier sites "elide the shared `--state-dir <run-state-dir>` for
brevity" — i.e. the file already knows it is repeating itself.

**Why obsolete.** Current models retain a once-stated conditional; seven restatements cost tokens
on the single file loaded at the start of every run and make the stage lines harder to scan for
the content that *is* unique to each stage (when the spoke applies, and what it owns).

**Action.** `rewrite` — move the "Deterministic helpers" receipt paragraph (`SKILL.md:103-105`)
up to just before the lifecycle stages under a `## Spoke receipts` heading, then reduce each
stage line to the spoke name and its trigger, e.g. "Before every routed role: spoke
`auto-routing`. Route the exact identity: …". Keeps every rule; removes ~180 words of
restatement from the hottest file in the repo.

---

## Flagged, no edit proposed (low confidence)

- **`skills/hermes-cli/SKILL.md`** is 18 lines of terse declarative sentences where the other
  three CLI primitives run 80-170 lines of failure modes and liveness caveats. Group 3 reads
  under-description as the more common defect and prescribes `add`, not `remove` — but nothing in
  the repo establishes whether Hermes has actually been exercised enough to have failure modes
  worth writing down, and inventing them would be worse than the gap. Flagged for the maintainer
  to answer.
- **`skills/auto-intake/SKILL.md:12-25`**, the twelve-item interview floor, pattern-matches
  Group 1c step-by-step choreography. Not proposed for removal: the twelve items are *context to
  elicit*, not a method for a judgment call, and the skill explicitly disclaims ordering ("one or
  two batched question rounds"). Recorded so a future audit does not re-open it.

## Applied

All eight findings were applied on the user's instruction. 15 files changed, one deleted, one test
updated.

### Verification

- `python3 scripts/check_ecosystem.py` — PASS (18 skills, 17 schemas, 22 evals; all within line
  budgets). `SKILL.md` went 124 → 126 lines against a budget of 128; the first pass overran at 138
  and the `## Spoke receipts` section was reflowed to the file's long-line convention.
- `python3 -m pytest -q` — 375 passed, 130 subtests. One test failed on the first run:
  `test_v3_instruction_contract.py::TestTrackingIssuePrecedesPlanning` asserted the literal string
  `check-spoke --state-dir <state_dir> --spoke auto-planning`, which F13 removed. Per Step 6
  ("a removal is complete only when everything referencing it goes too"), the assertion was
  repointed at the replacement phrasing `take the \`auto-planning\` receipt` — the ordering
  guarantee it tests is unchanged.

### Follow-ups left open

- `skills/hermes-cli/SKILL.md` under-description (flagged above) is unresolved and needs a
  maintainer who knows whether Hermes has been exercised enough to have documented failure modes.
- The `check-spoke`/`mark-spoke` phrasing now lives in one place. Any future test or hook that
  keys on the per-stage wording should key on the `## Spoke receipts` section instead.
