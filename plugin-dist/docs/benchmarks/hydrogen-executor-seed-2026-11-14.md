# Gemini 4 Hydrogen: gated executor seed replacement (2026-11-14)

Earlier owner direction staged Gemini 4 Hydrogen as a **conditional XS/S seed** replacing Gemini 3.8 Flash. The 2026-11-14 empirical report now makes its actual XS/S rank a **pending decision**, not approval to dispatch it. Claude Fable 5.2 is not an executor.

| Size | Old executor seed | New executor seed | Safety rationale |
| --- | --- | --- | --- |
| XS | agy/gemini-3.8-flash@low | agy/gemini-4-hydrogen@medium (conditional) | Hydrogen Medium is unmeasured; v5 Low 27 is not comparable with the v4 XS floor 31 |
| S | agy/gemini-3.8-flash@medium | agy/gemini-4-hydrogen@medium (conditional) | Unmeasured challenger; the empirical report instead favors Terra Medium as S first |

Hydrogen was released 2026-11-12 by Google. On **Artificial Analysis Intelligence Index v5.0.1** its published scores by effort are **Low 27, Medium 41, High 51, Xhigh 54**. Prices are **$1.50 input / $7.50 output per million tokens** (Google API rate, not Antigravity subscription usage). Sources:
- https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-hydrogen/
- https://deepmind.google/models/model-cards/gemini-4-hydrogen/
- https://artificialanalysis.ai/models/releases/gemini-4-hydrogen
- Third-party Antigravity model tier IDs (not Office conformance): https://github.com/AGI-is-going-to-arrive/Antigravity-Context-Window-Monitor/releases

**Index isolation:** the shipped index and size floors remain **AA v4.3.2**. Every new Hydrogen v4.3.2 benchmark score is **null**, since published v5.0.1 figures are not interchangeable with the pinned index. Do not fabricate a v4 score or infer that v5 score 41 passes the v4 floor. A deliberate v5 index migration and floor re-baseline is separate (#862). This is model registration / seed staging, **not** an accepted auto-update-benchmarks score refresh.

**Activation:** all four agy Hydrogen effort rows are `dispatchable: false`, `discovery: eligible`. Validate actual `agy models` catalog IDs and exact medium model/effort selection on the user's account, then isolated Office builder conformance, permissions, trust and quota, and the index migration. The shipped role policy gains **no Hydrogen allow**, because an allow can override disabled dispatchability. Conditional soft seeds cannot by themselves promote Hydrogen.

Keep all Flash catalog rows and previous allow/deny rules unchanged for historical dispatches and the existing **visual reviewer**; only replace the two XS/S executor seed entries. No changes to planner, review, L, XL, scores of existing models or routing floors. Fable remains outside executor seeds and can be considered separately as a planner.

## Empirical routing report addendum (2026-11-14; owner-provided)

Source: `report-executor-routing-token-economy-2026-11-14.md` uploaded in the decision conversation. The underlying local database, session logs, and scratch scripts were **not independently re-run** here. API list-price models are not measured subscription quota consumption.

- 1,019 reported tasks and 1,362 executor attempts overall; the 15-day sized period has 331 tasks. Token coverage is uneven (especially XS, XL and Terra S).
- **S:** measured Terra Medium 13 attempts / 77% landed, with costs for only five, compared to Gemini Flash Medium 55 attempts / 69% landed. The report proposes **Terra Medium → Sonnet Medium → Haiku High**, modeled $1.38 per chain vs $2.81 current. The Sonnet Medium S sample is only 3 attempts, so pilot rather than blindly switch.
- **M:** Sonnet Medium as the first route landed 18/18 tasks on rung one at $1.89 per full chain, versus Haiku High 74% of 65 first-route tasks at $5.05. Proposed **Sonnet Medium → Sonnet High → Terra High** needs a policy change; observational cohorts and unequal task mix still matter.
- **XS/L/XL:** XS Terra Low is thin and incompletely costed; L Sonnet High and XL Opus Medium remain measured first routes. Opus Medium as a conditional L fallback requires explicit authorization; it is currently XL-restricted.
- **Hydrogen:** **zero local executor attempts in this report**. Its official AA v5.0.1 headline Low 27 / Medium 41 / High 51 / Xhigh 54 figures cannot be compared with v4.3.2 gear floors or treated as measured coding success. **The previous claim that Low 27 fails the v4 XS floor 31 was invalid.**
- **Hydrogen staging:** keep all variants `dispatchable: false`, `discovery: eligible`, no role allow, no v4 floor waiver. Validate exact Antigravity model and effort readback, then run authorized, bounded, independently reviewed XS/S canaries versus Terra, Haiku and Sonnet. Capture accepted-work cost, first-pass and eventual land, RECHECKs, quota walls and token volume. An XS alternate or S rung behind Terra is a **hypothesis**, never an automatic seeded default today. This draft currently puts Hydrogen first at S; **do not merge it as an active first choice without reconciling that rank**.
- The report's two-RECHECK escalation is unvalidated judgment; test separately. Quota fallbacks must skip providers still in the same exhausted quota pool. M Sonnet High and Terra High need explicitly scoped policy allowances (Sonnet High at M should be **Claude Code only**, not agy through the combined rule); avoid weakening user overrides or independent review.

Tracking: [#862 v5 scoring research](https://github.com/Hikari9/auto-office/issues/862) and [draft #1142](https://github.com/Hikari9/auto-office/pull/1142). This addendum does **not** authorize dispatch, index cutover, or merge.
