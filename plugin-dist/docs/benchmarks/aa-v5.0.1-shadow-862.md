# Issue #862: AA v5.0.1 research-only evidence and Auto Office calibration

**November 14, 2026. Research-only; shadow mode. No production scoring/index/floor/trust/role/seed/trial-cap change or deployment.** Updated the same day with a local replay against real Office outcomes (section "Local replay").

Tracking: [#862](https://github.com/Hikari9/auto-office/issues/862); [approved executor policy #1143](https://github.com/Hikari9/auto-office/issues/1143); independent model PRs [Hydrogen #1142](https://github.com/Hikari9/auto-office/pull/1142) and [Fable #1133](https://github.com/Hikari9/auto-office/pull/1133).

## Evidence distinctions

| What it is | Stored as | Official AA Intelligence Index? |
|---|---|---|
| AA v5.0.1 headline, same exact model and effort | official_intelligence_index (integer) and intelligence_index_raw | **Yes**, v5.0.1 only |
| FrontierCode-AA 2.0 raw measurement | frontier_code2_percent | **No**, component |
| Terminal-Bench 4.0 raw measurement | terminal_bench4_percent | **No**, component |
| Official AA Engineering Index | engineering_index, same-date model capability snapshot | **No**; separate AA Engineering measure, separate edition **not published** |
| Auto Office coding score: 0.60 FC2 + 0.40 TB4 | coding-shadow.tsv, clearly marked SHADOW ONLY | **Never** |
| Auto Office quality, rework, cost, time, quota | Read-only local attributed dispatch/task episode export | No, empirical Office evidence |

Primary source methodology: [AA Intelligence benchmarking](https://artificialanalysis.ai/methodology/intelligence-benchmarking). Each exact model source URL, effort, public price, speed where published, UTC collection time, source HTML SHA-256, confidence, and null reason is in the frozen research/aa-v5.0.1/observations.jsonl dataset. The raw HTML is not redistributed (large dynamic third-party pages); its source URL and SHA-256 allow future integrity comparisons. A new web fetch can return newer scores; reproducibility uses the **committed numeric snapshot**, never an auto-refresh against a changing site.

The 26-row manifest covers the XS–XL route alternatives, Hydrogen Low/Medium/High/Xhigh and five separate planner-only Fable efforts. Twenty-five exact public model-effort measurement rows are present; **Argon Medium remains null** because the publication exposes Argon High, not an exact Medium result. There is no sibling-effort substitution. Engineering Index scores are official but their *distinct index revision is not identified* on these pages. Same-day comparison is possible without claiming Engineering Index v5.0.1.

The new research tools read **catalog/routes.tsv** to verify route identities, not retired seed.yaml. The existing production opt-in benchmark refresh in src/office/benchmarks.py already reads shipped_catalog.load() and candidates.catalog_rows() from the canonical TSV/YAML catalog; it stays unchanged and pinned to v4.3.2. New research code does not write catalog/benchmarks.tsv, catalog/manifest.yaml or a runtime snapshot.

## Key measured comparisons

| Route | FrontierCode 2.0 | Terminal-Bench 4.0 | Derived Office 60/40 | AA v5 official headline | AA Engineering |
|---|---:|---:|---:|---:|---:|
| Haiku 5.5 High | 23.20 | 21.72 | **22.61** | 30 | 36.50 |
| Sonnet 5.5 Medium | 10.97 | 29.80 | 18.50 | 31 | **37.29** |
| Terra 6.1 Medium | 34.54 | 32.32 | 33.65 | 38 | 42.61 |
| Terra 6.1 High | 37.46 | 41.92 | 39.24 | 42 | 46.42 |
| Sonnet 5.5 High | 25.74 | 43.94 | 33.02 | 42 | 46.19 |
| Hydrogen Low | 9.29 | 6.57 | 8.20 | 27 | 36.29 |
| Hydrogen Medium | 31.52 | 29.29 | 30.63 | 41 | 49.07 |
| Hydrogen High | 36.33 | 54.04 | 43.41 | 51 | 56.95 |
| Sol 6.1 Medium | 33.39 | 47.98 | 39.22 | 46 | 52.05 |
| Argon High | 25.71 | 57.07 | 38.25 | 50 | 57.30 |
| Opus 5.5 Medium | 33.08 | 52.53 | 40.86 | 48 | 53.75 |
| Fable 5.2 Max (PLANNER ONLY) | 48.76 | 59.60 | 53.09 | 65 | 72.64 |

**Official Engineering Index actually ranks Sonnet Medium above Haiku High: 37.29 vs 36.50.** The **Office-derived** 60/40 coding blend ranks them in reverse. This proves the benchmark selection materially affects an apparent winner, not actual accepted work. In S, Terra Medium has the higher fixed blend (33.65 vs 18.50 for Sonnet Medium); in M, Sonnet Medium is nonetheless the #1143 approved primary and the observational report favors it. Hydrogen variants have high public scores in some efforts but **no recorded Auto Office engineering outcomes here**. Argon/Sol XL evidence is too thin for a behavioral decision. Fable remains planner-only irrespective of numeric ranks.

Public references: [Haiku High](https://artificialanalysis.ai/models/claude-haiku-5-5-high), [Sonnet Medium](https://artificialanalysis.ai/models/claude-sonnet-5-5-medium), [Terra Medium](https://artificialanalysis.ai/models/gpt-6-1-terra-medium), [Hydrogen Medium](https://artificialanalysis.ai/models/gemini-4-hydrogen-medium), [Argon High](https://artificialanalysis.ai/models/gemini-4-argon), [Sol Medium](https://artificialanalysis.ai/models/gpt-6-1-sol-medium), [Fable release](https://artificialanalysis.ai/models/releases/claude-fable-5-2).

## Three factual calibration formulas and a yardstick

The **same executed attempts with exact same-route, same-effort v5 component measurements** feed every formula in `eval/aa_v5_calibrate.py`:

- **N no-benchmark yardstick:** the train+validation base rate. A formula that cannot beat N has not shown that the benchmark adds information.
- **A fixed 60/40:** benchmark blend through a deterministic **training-only** logistic link.
- **B domain blend:** FC2/TB4 weights per domain, shrunk to 0.6, regularization chosen on validation. Domains with too few attempts keep 0.6.
- **C weak benchmark prior + Office outcomes:** A's prediction as a Beta prior worth 1/2/4/8/12 pseudo-outcomes (chosen on validation), updated prequentially by earlier outcomes of the **same exact route** only. Other sizes and domains count at reduced weight; old outcomes decay (120-day half-life). Operational and plan-cancelled attempts are zero weight and reported separately.

**Purged chronological split.** Cuts sit at the 70% and 85% label-time quantiles. A run whose labels fall on both sides of a cut is dropped, so no run contributes to two windows and every training label precedes every validation label, which precedes every test label. (The first version merged overlapping runs into clusters; on real data 61 of 109 runs merged into one cluster, which left no usable holdout.) Test labels never tune anything. Metrics: held-out Brier, log loss, five-bin calibration error, plus paired 1,000-draw run-cluster bootstrap intervals for B-A, C-A, A-N, B-N and C-N. A formula is a **winner** only when its Brier delta against N has a 95% interval below zero. A and B use one score per exact route, so they can rank routes but cannot separate attempts within a route.

Every comparison runs twice: first-pass acceptance and eventual acceptance, on XS–XL-sized attempts and, as a sensitivity check, on all sizes.

## Local replay (2026-11-14)

All inputs were read from hash-verified private copies under a `chmod 700` directory outside the repository. Only aggregates are committed: `research/aa-v5.0.1/local-replay-2026-11-14.json`. It holds SHA-256 digests of the private inputs, no run, task, dispatch, session or account identifiers, and no paths.

### Snapshot and provenance

| Source | What was used | Integrity |
|---|---|---|
| `~/.local/share/auto-office/runs.db` | `cp` of `runs.db` + `runs.db-wal` while the source hash stayed constant, WAL checkpointed into the private copy, SQLite backup to one file. The live file was never opened by SQLite. 8,170 dispatches. | `integrity_check` ok; digest in the JSON |
| `rico-mode/state/executor-scoreboard.jsonl` | Current file: 1,027 tasks / 1,374 attempts. Regenerated at 02:49, after the report. | digest in the JSON |
| Report working files (`/tmp/sb`) | Frozen per-task attempt file the report's tables were computed from (`tasks-cost.json`), price table, extracted Claude/Codex usage. Survived on this machine and copied privately. | digests in the JSON |
| Claude Code transcripts | Raw session files **plus `subagents/` transcripts**, deduplicated by message id | read in place, read-only |

### Report reproduction

- **Population: reproduced.** The frozen file holds exactly **1,019 tasks / 1,362 attempts**. Every one is present and unchanged in today's scoreboard, which has 8 more tasks. runs.db had 8,149 dispatches at the report's cutoff and `money_actual`, `money_estimate` and `quota_delta` are NULL on all of them (still NULL on all 8,170).
- **Table 3.2 (tasks by first route): reproduced exactly** by `eval/aa_v5_report_replay.py`, including every 90% interval with the report's own bootstrap (LCG, seed 42, 4,000 draws). An independent Python bootstrap lands within $0.07 of each bound. M Sonnet Medium first: 18 tasks, 100% landed on rung 1, 61% one-shot, $1.89 (1.54–2.26). M Haiku High first: 65, 74%, 25%, $5.05 (4.04–6.14).
- **Claude cost: reproduced from raw transcripts.** 551 fully covered Claude attempts: report $1,938.97, raw transcripts $1,930.90 (0.4%); 542 within 5%. The match **requires subagent transcripts**. Without them, M Haiku High reads $2.88 per attempt instead of $3.40, because Haiku sessions spawn costly subagents.
- **Not independently re-derived:** Codex, pi, opencode and Antigravity token joins (working-directory and time joins), and Gemini cache fields decoded from protobuf. They are reproduced only as aggregates of the frozen file. Terra cost stays thin (Codex logs start 2026-11-06).

### Discrepancies and their causes

| # | Discrepancy | Cause | Effect |
|---|---|---|---|
| D1 | The PR's first extractor reported **0 RECHECKs on all 1,407 episodes** | `route_learning.derive_outcomes` counts only `code_review`/`CHANGES_REQUIRED`. This DB has no `code_review` gates; reviews are `convergence_review`/`RECHECK` (681), often shared across tasks via `members_json`. | Fixed in the research extractor. **Production `route_learning` has the same blind spot**; out of scope here, reported as a follow-up. |
| D2 | First extractor: **0 unknown sizes**; scoreboard: 688 unknown tasks | `route_learning` falls back to the **run's** risk size for 2,201 of 3,810 settled executor dispatches (58%) | The research extractor uses task-declared size only. Same production follow-up. |
| D3 | `claude/claude-opus-5-5@medium` episodes unmatched | The alias map dropped an exact key that collided with the legacy `claude/opus@medium` row | Fixed: exact keys always resolve; alias rows go to their pinned invocation |
| D4 | 158 Gemini Flash and 13 Antigravity Sonnet attempts unmatched | Antigravity records invocation spellings (`gemini-3.8-flash-medium`, `Claude Sonnet 5.5 (High)`) | Fixed: unambiguous invocation spellings resolve to their one route |
| D5 | The report counts `claude/sonnet@*` as Sonnet 5.5 | The catalog's floating `sonnet` alias invokes `claude-sonnet-5`, not 5.5 | Excluded from exact-score analysis (10 attempts). The report's M Sonnet Medium cohort is 16 exact-route tasks here, not 18. |
| D6 | The report's cohorts exclude open runs | The scoreboard emits only tasks in closed or abandoned runs. It also falls back to `immutable=1`, which ignores the WAL. | 48 settled tasks in still-open runs are missing from the report. They are weaker: M Sonnet Medium first route 10 more tasks at 20% first-pass / 80% eventual (report population: 16 at 56% / 100%). M Haiku High: 14 more at 14% / 57% (report: 65 at 25% / 74%). The ordering holds. The 18/18 landing rate does not. |
| D7 | "Land" rates differ slightly | The report's land is accepted / (accepted + rerouted). Here eventual is accepted / all attempts, including abandoned. | Small. Definitions are in the JSON. |

The research extractor reproduces the report population exactly. On the 1,019 frozen tasks, **all 1,362 attempts match** on dispatch list, outcome and RECHECK count. 3 attempts differ on size: those tasks carry two declared sizes, and the extractor says unknown.

### Measured outcomes (exact routes, all positions, ≥3 attempts)

Estimated cost is list-price token cost from the report's join, executor plus charged review. It is **not** subscription quota and not `money_actual`, which is never recorded. "Indep." is the share of accepted attempts approved by a gate recorded `independent` whose reviewer model family differs from the executor's.

| Size | Exact route | 60/40 | n | First-pass | Eventual | Mean RECHECK | Op fail | Est. $/attempt (costed n) | Exec min | Indep. (n) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| XS | codex/gpt-6.1-terra@low | 20.75 | 8 | 50% | 50% | 0 | 4 | 0.20 (1) | 2.7 | null |
| XS | claude/claude-haiku-5-5@high | 22.61 | 5 | 80% | 100% | 0.6 | 0 | 0.47 (5) | 48.3 | 100% (5) |
| S | claude/claude-haiku-5-5@high | 22.61 | 81 | 33% | 88% | 1.37 | 6 | 1.97 (65) | 115.1 | 100% (63) |
| S | agy/gemini-3.8-flash@medium | 17.76 | 62 | 23% | 61% | 1.16 | 14 | 2.70 (49) | 56.9 | 97% (32) |
| S | codex/gpt-6.1-terra@medium | 33.65 | 15 | 53% | 67% | 0.53 | 3 | 1.37 (5) | 5.5 | 100% (8) |
| S | claude/claude-sonnet-5-5@high | 33.02 | 10 | 20% | 50% | 1.5 | 2 | 2.67 (9) | 18.5 | 100% (5) |
| S | claude/claude-sonnet-5-5@medium | 18.50 | 4 | 25% | 50% | 1.75 | 1 | 3.31 (2) | 19.5 | 100% (2) |
| M | claude/claude-haiku-5-5@high | 22.61 | 98 | 21% | 69% | 1.68 | 17 | 3.96 (73) | 92.4 | 100% (53) |
| M | agy/gemini-3.8-flash@medium | 17.76 | 39 | 10% | 41% | 1.77 | 8 | 2.32 (32) | 36.6 | 94% (16) |
| M | claude/claude-sonnet-5-5@medium | 18.50 | 36 | 31% | 86% | 2.19 | 1 | 2.61 (22) | 43.9 | 100% (25) |
| M | codex/gpt-6.1-terra@medium | 33.65 | 35 | 11% | 29% | 0.71 | 15 | 1.26 (18) | 19.9 | 100% (10) |
| M | claude/claude-sonnet-5-5@high | 33.02 | 23 | 30% | 65% | 0.96 | 5 | 3.81 (18) | 60.0 | 100% (15) |
| M | codex/gpt-6-luna@high | 12.98 | 11 | 0% | 45% | 1.73 | 4 | 0.11 (2) | 57.3 | **0% (5)** |
| M | codex/gpt-6.1-terra@high | 39.24 | 5 | 40% | 100% | 1.0 | 0 | null (0) | 125.0 | 100% (5) |
| L | claude/claude-sonnet-5-5@high | 33.02 | 45 | 36% | 91% | 1.8 | 3 | 10.17 (42) | 62.3 | 100% (40) |

Across all 1,451 settled attempts: 31% first-pass, 72% eventual, 51% needed at least one RECHECK (mean 1.15), 255 operational failures, 143 quality reroutes or abandons after RECHECK, 60 usage-limit hits in 1,357 attempts with that flag. Mean executor time is 61 minutes. Estimated cost covers 789 attempts ($3.18 per attempt).

- **Unsupported, left null:** regression rate, `money_actual`, quota consumption. `outcome_labels` has only `verified_no_observed_failure` and `abandoned`. No label can record a regression, so a regression rate cannot be estimated.
- **Quota:** only usage-limit hits are observable, not consumption.
- **Reviewer independence:** Luna executors are approved by Luna reviewers, so 0% of their accepted work had cross-family review (report caveat C9, confirmed).

### A/B/C results: no winner

| Scope : target | Train / val / test attempts (test runs) | N | A | B | C | C−N 95% CI | A−N 95% CI |
|---|---|---:|---:|---:|---:|---|---|
| XS–XL : first-pass | 270 / 39 / 43 (17) | **0.272** | 0.292 | 0.292 | 0.278 | −0.028 to 0.035 | −0.020 to 0.080 |
| XS–XL : eventual | 270 / 39 / 43 (17) | 0.089 | 0.096 | 0.096 | **0.082** | −0.014 to 0.003 | −0.002 to 0.016 |
| all sizes : first-pass | 764 / 148 / 158 (46) | 0.224 | **0.218** | 0.218 | 0.230 | −0.016 to 0.028 | −0.024 to 0.013 |
| all sizes : eventual | 764 / 148 / 158 (46) | 0.136 | 0.133 | 0.133 | **0.124** | −0.037 to 0.011 | −0.008 to 0.001 |

Values are held-out Brier; lower is better.

- **No formula beats the no-benchmark yardstick at 95% in any scope or target.** The point leader changes with the target, and in the main sized first-pass comparison N itself is lowest.
- **B is degenerate:** no task or dispatch records a domain, so B equals A.
- **Coverage is thin:** the sized test window holds 43 attempts from 17 runs, with no Terra, Hydrogen, Sol or Argon attempts and 2 XL attempts.
- **The 60/40 order partly agrees with observed order:** first-pass Spearman is 0.68 over 8 M routes and 0.40 over 4 S routes. Eventual is 0.34 and 0.00. L and XL have too few routes to say.
- **Haiku High vs Sonnet Medium:** the benchmark ranks Haiku High above Sonnet Medium (22.6 vs 18.5). At M, observed outcomes put them the other way (eventual 69% vs 86%). The official Engineering Index agrees with the outcomes.

### Replay against #1143's approved soft preferences (factual only)

| Size | Approved rungs with factual attempts at that size | Never executed at that size (unobserved) | Tasks whose first route already was an approved rung-1 route |
|---|---|---|---|
| XS | Terra Low (8: 50% eventual, 4 operational) | Haiku Medium, Hydrogen Low | 0 of 13 |
| S | Terra Medium (15: 53% first-pass, 67% eventual), Sonnet Medium (4) | Hydrogen Medium | 10 of 135 |
| M | Sonnet Medium (36: 31% / 86%), Terra High (5: 40% / 100%) | Hydrogen High | 26 of 172 |
| L | Sonnet High (45: 36% / 91%) | Hydrogen High | 39 of 47 |
| XL | Opus Medium (2) | Sol Medium, Argon High | 2 of 3 |

- Hydrogen has **zero** local attempts at any size, so the S/M Hydrogen canary is entirely unmeasured.
- The unobserved routes are not scored. No task that ran another route is relabeled as a success or failure for an approved route.
- The data supports the direction of #1143 at M (Sonnet Medium over Haiku High) and L (Sonnet High). It is too thin to support or contradict S Terra Medium (15 attempts, 5 costed), XS or XL.

## Reproduce

From a checkout, the committed evidence and tests:

    python eval/aa_v5_evidence.py validate
    python -m pytest -q tests/test_aa_v5_research.py tests/test_shipped_catalog_tables.py tests/v31/test_benchmark_refresh.py tests/v31/test_intelligence_floor.py

Private local replay. The outputs contain identifiers: keep them in the private directory and never commit or upload them.

    P=~/private/office-aa-862; mkdir -p $P/src; chmod 700 $P
    S=~/.local/share/auto-office
    shasum -a 256 $S/runs.db $S/runs.db-wal > $P/before
    cp $S/runs.db $S/runs.db-wal $P/src/
    shasum -a 256 $S/runs.db $S/runs.db-wal | diff - $P/before   # must be empty, else repeat
    python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); s.execute('PRAGMA wal_checkpoint(TRUNCATE)'); d=sqlite3.connect(sys.argv[2]); s.backup(d)" $P/src/runs.db $P/runs-copy.db
    python eval/aa_v5_outcomes.py --db $P/runs-copy.db --out $P/attempts.jsonl [--attempt-costs $P/tasks-cost.json]
    python eval/aa_v5_calibrate.py --outcomes $P/attempts.jsonl --report $P/shadow-results.json
    python eval/aa_v5_report_replay.py --tasks-cost $P/tasks-cost.json --scoreboard <scoreboard copy> --db $P/runs-copy.db --prices $P/prices.json

`--attempt-costs` takes the report's per-task attempt file. Without it, every cost field stays null. The report's working directory survived on this machine. On another machine, the cost join must be rebuilt from the harness session logs; the report's section 6 describes the method.

The importer fetches from public AA only on explicit command (`python eval/aa_v5_evidence.py collect`). A refresh needs a new snapshot date and never updates production scores.

## Floor and prior shadow recalibration

The old research-only proposed coding floors XS/S/M/L/XL **8/17/22/33/38** and soft ceilings 20/28/36/44/unbounded are **not authorized**. The exact #1143 soft seed rung replay exposes the conflict:

| Size | Old shadow floor | Lowest scored approved route | Maximum floor just to include every measured soft seed | Warning |
|---|---:|---|---:|---|
| XS | 8 | Hydrogen Low: 8.20 | 8 | Hydrogen XS manual only |
| S | 17 | Sonnet Medium: 18.50 | 18 | Narrow score margin |
| **M** | **22** | **Sonnet Medium: 18.50** | **18** | **Old v5 floor blocks owner-approved M primary** |
| L | 33 | Sonnet High: 33.02 | 33 | Almost no margin |
| XL | 38 | Argon High: 38.25 | 38 | Sparse real XL evidence |

These are **mechanical maximum coverage thresholds**, not quality or safety floors.

- **The local replay argues against a v5 coding floor at M.** The route that performs best there has the lowest 60/40 score among the approved routes.
- **A benchmark prior adds no measurable predictive value** over a base rate on this data, so a v5 prior strength cannot be fitted.
- **What would change that:** domain labels, Hydrogen/Sol/Argon attempts, and more XS/XL tasks.
- **Unchanged:** v4 floors XS31/S38/M40/L44/XL47 and the adaptive benchmark priors.

**Tradeoffs:**

- A is simple and low-variance but cannot tell frontier coding from terminal agency.
- B could separate domains but has no domain data.
- C learns route-level accepted-work behavior and is the natural shape for letting Office outcomes outrank benchmarks. It still suffers from nonrandom routing, shared-gate RECHECK attribution, quota gaps and small XS/XL cohorts.

A shadow ranking is **not** an instruction to alter the approved seeds or trial caps.

## CI, rollback and owner decision

`.github/workflows/aa-v5-shadow-research.yml` is a narrow offline workflow: no AA network calls, no live dispatch, no deployment. Remote CI is off for this repository, so the same commands run locally through `scripts/validate.sh --pr`.

- **Validates:** exact edition, effort, provenance and null handling; the pinned v4 catalog; the fixed #1143 shadow rungs; fixture replay; refusal to score without outcomes.
- **Untouched:** runtime code, catalog benchmark tables, role eligibility, invocation slugs, review gates and trusted scores.

**Recommendation:** keep production v4.3.2 and #1143. Keep this research as a reproducible shadow tool.

**Follow-ups outside this PR's authority:**

- `route_learning` RECHECK counting (D1) and run-size fallback (D2). Both bias live adaptive learning.
- Domain labels on tasks.
- A regression-positive outcome label.
- Recorded per-dispatch cost and quota.
- Making the scoreboard include settled tasks in open runs and read through the WAL.

**Owner cutover decision remaining:** whether to migrate executor scoring to AA v5 at all. On this evidence the answer is not yet. No formula beats a base rate, and the M primary would fail a v5 coding floor. Do not merge a v5 scoring migration or close #862 as fully migrated without owner approval.

Rollback of this research addition is a standard revert of its research-only files. No production index schema migration exists. A later v5 production change would need a new index or policy snapshot pinned per run, with a separately verified rollback to v4.
