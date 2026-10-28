# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

## 3.10.0

- feat(catalog): move the shipped model seed to strict, normalized TSV routing and benchmark tables plus YAML provenance; keep legacy in-memory objects, run hashes, lineages, user overlays and policy unchanged (#852).

## 3.9.0

- feat(routing): a user role allow (or exact `preferred_seed` entry) makes a route dispatchable with no proof, qualification or trust gate and overrides a catalog or user-overlay `dispatchable: false`; a runtime route failure is logged (`route.failed`), demoted for a TTL and falls back to the next eligible route; `office route policy --effective` shows what dispatches and why, and `office route allow a,b` reshuffles several routes at once (#845).
- feat(routing): committed role policy is size-scoped allow / neutral / deny from one data file, `catalog/role-policy.yaml` (#850 sections 1-3). Executor follows the #809 slate by size (Haiku, Gemini 3.8 Flash, GPT-6.1 Terra, Sonnet 5.5 on claude and agy, XL Opus on claude only, GPT-6.1 Sol, conditional GLM) and denies Astra and 5.6 Sol/Terra; code and plan reviewers are Luna high > DeepSeek max > Opus low (claude, then agy) with Sonnet medium/high and Opus above low denied; the visual reviewer is Gemini 3.8 Flash low/medium and GPT-6.1 Terra low. User `role_rules` accept `sizes`, `office route allow|neutral|deny|clear --size XS,S` writes them and `office route policy --effective --size XL` shows one size.
