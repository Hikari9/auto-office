# GPT-6.1 Terra registration and 6.1 generation preference (2026-10-27)

User-requested new model registration plus executable generation-aware supersession: after all existing factual routing gates, qualified 6.1 Sol/Terra replaces 5.6 of the SAME lineage, harness and effort in automatic executor/worker candidate selection. Older 5.6 is retained and eligible when the newer exact route fails, is excluded, is unavailable or lacks that effort. Manual routes and explicit old-generation user preferences are preserved. Cross-lineage selection remains adaptive; XS–XL task slates are owned by #809, not hard-coded into this registration PR. No existing scores or trust settings changed. Terra remains discovery-eligible and non-dispatchable until exact local Codex invocation/effort conformance is proven.

Benchmark: Artificial Analysis Intelligence Index v4.3.2. Fetched 2026-10-27T04:29:56Z. Model release: https://openai.com/index/introducing-gpt-6-1-terra/ . Model documentation: https://developers.openai.com/api/docs/models/gpt-6.1-terra . AA family: https://artificialanalysis.ai/models/releases/gpt-6-1-terra .

| Office effort | AA effort | Score | Exact AA page |
|---|---|---:|---|
| max | max | 48 | https://artificialanalysis.ai/models/gpt-6-1-terra |
| xhigh | xhigh | 45 | https://artificialanalysis.ai/models/gpt-6-1-terra-xhigh |
| high | high | 44 | https://artificialanalysis.ai/models/gpt-6-1-terra-high |
| medium | medium | 41 | https://artificialanalysis.ai/models/gpt-6-1-terra-medium |
| low | low | 36 | https://artificialanalysis.ai/models/gpt-6-1-terra-low |
| none | non-reasoning | 33 | https://artificialanalysis.ai/models/gpt-6-1-terra-non-reasoning |

Known pricing: USD 1 input and USD 5 output per million tokens. Unknown speed is null. Benchmark-only evidence does not verify Codex invocations. Non-reasoning maps to Office none but still requires conformance.

Validation updated 2026-10-27: standalone PR #811 at `5da71eb745e9ddaa8a27a299b1b690bb38dded33` passed 28 selected benchmark/routing tests and `scripts/check_ecosystem.py` in isolated Vercel Sandbox (exit 0). The six exact AA Intelligence Index v4.3.2 source pages above were checked against the published scores. An isolated, UNPUSHED compatibility merge with #809 at `b471019076dd22543c7346157a7d39277d8edae0` passed 62 combined tests (exit 0); it exposed one trivial merge conflict in `src/office/adaptive.py` audit keys, resolved in that disposable checkout by preserving BOTH `lineage_supersession` and `sonnet_self_fallbacks`. This is test evidence, not release approval. Full `scripts/validate.sh --all` and real Codex conformance have not passed; GitHub Actions are absent by maintainer decision #120. Keep PR draft until applicable full gate, independent final-SHA review, and release approvals pass.
