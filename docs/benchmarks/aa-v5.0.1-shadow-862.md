# Issue #862: AA v5.0.1 research-only evidence and Auto Office calibration

**November 14, 2026. Research-only; shadow mode. No production scoring/index/floor/trust/role/seed/trial-cap change or deployment.**

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

## Three factual calibration formulas

The **same eligible, actual executed task-route episodes with exact v5 component measurements** feed all three formulas in eval/aa_v5_calibrate.py:

1. **A fixed 60/40:** benchmark blend passed through a deterministic **training-only** calibrated logistic probability link to first-pass acceptance.
2. **B domain blend:** learned weights on FC2 vs TB4, shrunk to 0.6 with a regularization penalty; insufficient-domain cohorts retain 0.6. Fit on training runs, choose regularization on a chronological validation holdout.
3. **C weak benchmark prior + Office outcomes:** use A's trained prediction as a Beta prior worth 1/2/4/8/12 pseudo-outcomes; choose strength on validation. Historical outcomes for the **same harness, invocation model and effort** update predictions chronologically. Cross-size or cross-domain episodes carry discounted weight; old episodes decay (120-day half-life). Failed launches, quota/environment/reviewer failures are zero-weight for *model-quality* learning and reported separately.

**Run-disjoint, non-overlapping chronological windows:** if a slow run overlaps another run, the overlap forms one unsplittable time cluster so future acceptance cannot leak into training. Test labels are never used to tune A/B/C; C is evaluated prequentially, updating only after an observed outcome. Compare held-out Brier score, log loss, five-bin calibration error, empirical success, and (at least three test runs) paired 1,000-draw run-cluster bootstrap intervals. **Unexecuted alternative executor choices are not labeled as successes or failures.** A comparison may be unavailable if components, effort identity, data authority or chronological runs are insufficient.

The local extractor eval/aa_v5_outcomes.py reads a consistent SQLite copy with mode=ro and PRAGMA query_only=ON, never the schema-migrating Office db.connect. It hashes the file before and after, reuses route_learning.derive_outcomes and episodes, resolves invocation identity using current routes.tsv, keeps private IDs only in the locally written JSONL, and preserves missing cost/quota/regression/reviewer facts as null. First-pass means first accepted attempt without code-review RECHECK, eventual success means a landed task-route episode. Code-review presence alone **does not verify reviewer independence**; regression labels remain unknown without independent source evidence. Public AA speed is not local runtime duration or provider uptime.

### Empirical report: secondary until underlying records are accessible

The user-provided **Executor routing by task size: token-economy evidence (2026-11-14)** reports 1,019 tasks and 1,362 attempts in rico-mode/state/executor-scoreboard.jsonl, plus local ~/.local/share/auto-office/runs.db. It says all 8,149 Office dispatches have blank money_actual, money_estimate and quota_delta; the reported USD estimate instead joins multiple provider session logs with list API prices. The report described 4,000 task bootstrap resamples but **neither underlying task outcomes nor the random seeds/outputs** are available in the present environment.

Reported nonrandomized M first-route cohorts: Sonnet Medium 18 tasks, 100% route land, 61% one-shot and estimated $1.89 per task; Haiku High 65 tasks, 74% land, 25% one-shot and estimated $5.05 per task. These are **reported observational associations**, **not independently reproduced causation**. The Terra Medium S cohort is thin (13 attempts, five fully costed); Gemini cache cost semantics were inferred. Provider quota pools are distinct; list-price USD spend is **not** subscription quota consumption. The observational report cannot decide the owner-approved #1143 policy.

**Exact blocker:** the local scoreboards, present-day runs.db and harness transcript files are **not checked into these repositories or mounted as Library files**. The private rico-mode repository did not expose the specified state JSONL at its probed location. The frozen research/aa-v5.0.1/shadow-blocked.json therefore reads **blocked_no_raw_outcomes**: no locally trained A/B/C winner, validated cost reduction or quota adjustment can honestly be produced. Fixtures in the unit tests are **synthetic for code validation only** and never used as research evidence.

## Reproduce and complete local replay

From a checkout:

    python eval/aa_v5_evidence.py validate
    python eval/aa_v5_evidence.py summary
    python eval/aa_v5_evidence.py export
    python -m pytest -q tests/test_aa_v5_research.py tests/test_shipped_catalog_tables.py tests/v31/test_benchmark_refresh.py tests/v31/test_intelligence_floor.py

To capture a **consistent** local runs.db without modifying it, create a private backup:

    mkdir -p ~/private/office-aa-862
    chmod 700 ~/private/office-aa-862
    python - <<'PY'
    import sqlite3
    from pathlib import Path
    source = Path.home()/".local/share/auto-office/runs.db"
    target = Path.home()/"private/office-aa-862/runs-copy.db"
    with sqlite3.connect(f"file:{source}?mode=ro",uri=True) as original:
        with sqlite3.connect(target) as copy:
            original.backup(copy)
    print(target)
    PY

Then read only and evaluate real outcomes:

    python eval/aa_v5_outcomes.py --db ~/private/office-aa-862/runs-copy.db --out ~/private/office-aa-862/episodes.jsonl
    python eval/aa_v5_calibrate.py --outcomes ~/private/office-aa-862/episodes.jsonl --report ~/private/office-aa-862/shadow-results.json
    sha256sum ~/private/office-aa-862/runs-copy.db

The local scoreboard and provider token session logs need separate, authorized, timestamp-joined extraction to reproduce the original report's **larger population, full chain reviewer costs, price-cache sensitivity, and task-bootstrap counts**. Until then these calculations remain reported rather than independently verified. **Never commit the private DB, episodes containing IDs, session logs, quota/account data or the private report with identifiers.** The frozen public observations are safe to commit. The importer collects from public AA only on explicit command:

    python eval/aa_v5_evidence.py collect

This network call may change evidence. Record a distinct snapshot/date for any later refresh. It never updates production scores.

## Floor and prior shadow recalibration

The old research-only proposed coding floors XS/S/M/L/XL **8/17/22/33/38**, soft ceilings 20/28/36/44/unbounded are **not authorized**. The exact #1143 soft seed rung replay exposes the conflict:

| Size | Old shadow floor | Lowest scored approved route | Maximum floor just to include every measured soft seed | Warning |
|---|---:|---|---:|---|
| XS | 8 | Hydrogen Low: 8.20 | 8 | Hydrogen XS manual only |
| S | 17 | Sonnet Medium: 18.50 | 18 | Narrow score margin |
| **M** | **22** | **Sonnet Medium: 18.50** | **18** | **Old v5 floor blocks owner-approved M primary** |
| L | 33 | Sonnet High: 33.02 | 33 | Almost no margin |
| XL | 38 | Argon High: 38.25 | 38 | Sparse real XL evidence |

These are **mechanical maximum coverage thresholds**, not proposed quality/safety floors. Even "M=18" would merely prevent one contradiction, not show that M tasks succeed reliably. A production v5 midpoint/scale/prior strength or size floors **cannot be fitted without genuine task outcomes and held-out replay**. Existing v4 floors XS31/S38/M40/L44/XL47 and adaptive benchmark priors remain unchanged.

**Tradeoffs:** A is simple and low-variance but misses differences between frontier coding and terminal agency; B might explain domains but may overfit; C might learn route-level accepted-work/recheck behavior and let genuine Office outcomes outrank benchmarks, but suffers from nonrandom routing, correlated tasks, reviewer attribution, quota gaps, missing token cost and small XS/XL cohorts. A shadow "winner" is **not** an instruction to alter the approved seeds or trial caps.

## CI, rollback and owner decision

The narrow, offline GitHub Actions workflow .github/workflows/aa-v5-shadow-research.yml validates exact edition/effort/provenance/null handling, pinned v4 catalog integrity, fixed #1143 shadow rungs, successful fixture replay and **deliberate failure to score with no observed outcomes**. CI makes no AA network calls, no live dispatch and no deployment. Tests are mirrored by the documented commands. No runtime code, catalog benchmark table, role eligibility, invocation model slug, review gate or trusted score is edited.

**Recommendation:** keep production v4.3.2 and #1143; add this reproducible shadow research for future genuine local calibration. Before seeking a separate owner cutover approval, obtain chronological run-attributed outcomes and adequate size/domain coverage, independent reviewer/regression evidence, task-level time/estimated cost/quota data with attribution, safe successful Hydrogen exact-effort conformance, shadow-to-factual accepted-work comparisons, and explicit v4-pinned-run rollback tests. **Do not merge a v5 scoring migration or close #862 as fully migrated without owner approval.**

Rollback of this research addition is a standard revert of its research-only files; no production index schema migration exists. A later v5 production change requires a new index/policy snapshot pinned per run and separate verified rollback to the existing v4 version.
