# Gemini 4 Hydrogen: gated executor seed replacement (2026-11-14)

Owner decision: replace Gemini 3.8 Flash with **Gemini 4 Hydrogen** as a preferred executor **seed** for XS/S, not as a verified automatic executor. Claude Fable 5.2 is not an executor.

| Size | Old executor seed | New executor seed | Safety rationale |
| --- | --- | --- | --- |
| XS | agy/gemini-3.8-flash@low | agy/gemini-4-hydrogen@medium (conditional) | AA v5 Low = 27, below the numerically similar XS floor 31 |
| S | agy/gemini-3.8-flash@medium | agy/gemini-4-hydrogen@medium (conditional) | AA v5 Medium = 41, with local qualification pending |

Hydrogen was released 2026-11-12 by Google. On **Artificial Analysis Intelligence Index v5.0.1** its published scores by effort are **Low 27, Medium 41, High 51, Xhigh 54**. Prices are **$1.50 input / $7.50 output per million tokens** (Google API rate, not Antigravity subscription usage). Sources:
- https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-hydrogen/
- https://deepmind.google/models/model-cards/gemini-4-hydrogen/
- https://artificialanalysis.ai/models/releases/gemini-4-hydrogen
- Third-party Antigravity model tier IDs (not Office conformance): https://github.com/AGI-is-going-to-arrive/Antigravity-Context-Window-Monitor/releases

**Index isolation:** the shipped index and size floors remain **AA v4.3.2**. Every new Hydrogen v4.3.2 benchmark score is **null**, since published v5.0.1 figures are not interchangeable with the pinned index. Do not fabricate a v4 score or infer that v5 score 41 passes the v4 floor. A deliberate v5 index migration and floor re-baseline is separate (#862). This is model registration / seed staging, **not** an accepted auto-update-benchmarks score refresh.

**Activation:** all four agy Hydrogen effort rows are `dispatchable: false`, `discovery: eligible`. Validate actual `agy models` catalog IDs and exact medium model/effort selection on the user's account, then isolated Office builder conformance, permissions, trust and quota, and the index migration. The shipped role policy gains **no Hydrogen allow**, because an allow can override disabled dispatchability. Conditional soft seeds cannot by themselves promote Hydrogen.

Keep all Flash catalog rows and previous allow/deny rules unchanged for historical dispatches and the existing **visual reviewer**; only replace the two XS/S executor seed entries. No changes to planner, review, L, XL, scores of existing models or routing floors. Fable remains outside executor seeds and can be considered separately as a planner.
