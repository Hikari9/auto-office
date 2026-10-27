# GPT-6.1 Terra registration and 6.1-over-5.6 lineage supersession (2026-10-27)

## Routing behavior

The medium-only `roles.executor.preferred_seed` from the first commit of #811 is removed. Executor and worker routing (`office.adaptive.recommend`) now applies lineage supersession after the utility ranking, the close-call draw and exploration:

- For the Sol and Terra lineages (`model_lineages.EXECUTOR_SUPERSESSION_LINEAGES`), the predecessor of a newer released generation that *qualified* on the same harness and effort moves to directly behind it. The newer generation keeps its own utility rank, and the predecessor is the next route after it, so whenever 6.1 leads, 5.6 is in the slate as a fallback that a planned dispatch tries without a reroute. Other lineages, Luna included, are not affected.
- Only qualifying rows count. A 6.1 route that is denied, size-scoped overkill, untrusted, missing a capability, below the floor, over the quota reserve, rate-limited, learned-ineligible, held out by a gear's seed retention, or still in discovery never enters the comparison, and 5.6 keeps its own rank.
- An effort the newer generation lacks is never invented. 6.1 Sol has no `none` row, so Sol@none stays 5.6.
- It runs after the budget ceiling, so an unaffordable 6.1 route never demotes an affordable 5.6 one.
- A manual, declared (`--as`) or exact route turns supersession off. A predecessor the effective `preferred_seed` (request or policy) ranks ahead of its successor is not moved.
- Routes of different lineages are compared on their own utility. 6.1 never inherits 5.6's place, so a stronger other lineage still outranks it, and Sol is not forced above Terra.
- The audit record carries `lineage_supersession` (each pair: route, supersedes, lineage, effort), included in `decision_hash`, and the slate reasons name it. A decision with no pair hashes as before.

Tests: `tests/v31/test_lineage_supersession_811.py` and `tests/v31/test_issue811_terra_supersession.py`.

### Reconciliation with the parallel implementation

Commits `5da71eb`..`f985580` (pushed concurrently) implemented supersession by removing 5.6 from the slate when 6.1 qualified, so 5.6 returned only when routing recomputed. Rico chose to merge: the reorder above keeps 5.6 in the slate, and from that work it keeps the explicit Sol/Terra allowlist, the same-harness rule, the manual/declared/exact-route opt-out, the after-budget-ceiling ordering and the `lineage_supersession` audit key. Its `prefer_qualified_generation` filter is replaced and its tests are adapted to the in-slate fallback. Its validation record, kept verbatim: standalone PR #811 at `5da71eb745e9ddaa8a27a299b1b690bb38dded33` passed 28 selected benchmark/routing tests and `scripts/check_ecosystem.py` in an isolated Vercel Sandbox (exit 0). An isolated, unpushed compatibility merge with #809 at `b471019076dd22543c7346157a7d39277d8edae0` passed 62 combined tests (exit 0) and exposed one trivial merge conflict in `src/office/adaptive.py` audit keys, resolved by preserving both `lineage_supersession` and `sonnet_self_fallbacks`.

### Independent review round 1 (`1c264f4`)

Codex `gpt-6.1-sol` (high, read-only) reviewed `origin/main...1c264f4` and raised four findings, all fixed in the next commit with a test that fails when the fix is reverted:

| Code | Finding | Fix | Test |
|---|---|---|---|
| R1 | 6.1 routes that already outranked 5.6 could fill all three slate slots, leaving no 5.6 fallback | Supersession now moves 5.6 directly behind its 6.1 successor | `test_older_generation_stays_in_the_slate_when_newer_routes_fill_the_top` |
| R2 | `policy.preferred_seed` was ignored by the seed guard | Guard reads the effective seed (request, else policy), as `routing.route` does | `test_policy_seeded_older_route_is_not_superseded` |
| R3 | 6.1 inherited 5.6's place and could outrank a stronger other lineage | Same change as R1: 6.1 keeps its own utility rank | `test_adaptive_prefers_newer_only_within_comparable_qualified_lineage` (exact order) |
| R4 | Hash test passed with the record stripped from the hash | Test now withholds only the record and requires a different hash | `test_decision_hash_covers_the_supersession_record` |

## Terra AA verification

Benchmark: Artificial Analysis Intelligence Index v4.3.2. Scores first fetched 2026-10-27T04:29:56Z, rechecked against each page on 2026-10-27 around 04:58Z. All six scores and the price matched, so none changed. Speed was null in the first commit and is now filled from the same pages (AA first-party API measurement). Model release: https://openai.com/index/introducing-gpt-6-1-terra/ . Model documentation: https://developers.openai.com/api/docs/models/gpt-6.1-terra . AA family: https://artificialanalysis.ai/models/releases/gpt-6-1-terra .

| Office effort | AA page title | Score | Output tok/s | TTFT | Exact AA page |
|---|---|---:|---:|---:|---|
| max | GPT-6.1 Terra (max) | 48 | 90.9 | 77.96 s | https://artificialanalysis.ai/models/gpt-6-1-terra |
| xhigh | GPT-6.1 Terra (xhigh) | 45 | 90.9 | 13.32 s | https://artificialanalysis.ai/models/gpt-6-1-terra-xhigh |
| high | GPT-6.1 Terra (high) | 44 | 90.9 | 5.66 s | https://artificialanalysis.ai/models/gpt-6-1-terra-high |
| medium | GPT-6.1 Terra (medium) | 41 | 90.9 | 1.43 s | https://artificialanalysis.ai/models/gpt-6-1-terra-medium |
| low | GPT-6.1 Terra (low) | 36 | 91.0 | 1.28 s | https://artificialanalysis.ai/models/gpt-6-1-terra-low |
| none | GPT-6.1 Terra (non-reasoning) | 33 | 91.1 | 1.27 s | https://artificialanalysis.ai/models/gpt-6-1-terra-non-reasoning |

Pricing on every page: USD 1.00 input and USD 5.00 output per million tokens. Each page says "Released October 2026".

## Codex smoke receipts

`codex debug models` (codex-cli 0.163.0, live list) lists `gpt-6.1-terra` with efforts low, medium, high, xhigh, max and ultra. It does not list `none`. The bundled list does not include `gpt-6.1-terra`.

Each effort was run once in an empty temp directory with `codex exec --json --skip-git-repo-check --sandbox read-only -m gpt-6.1-terra -c model_reasoning_effort=<effort> "Reply with exactly: TERRA-OK"`. Identity is read back from the Codex session rollout (`turn_context.model`/`effort`), with the server `response_id` from its `token_usage_record`. These are harness settings readbacks, not a server-reported model name.

| Effort | Exit | Reply | Readback model / effort | Thread | Response id |
|---|---|---|---|---|---|
| low | 0 | TERRA-OK | gpt-6.1-terra / low | 01a17bba-6e1c-7f80-997e-77071a66f5c9 | resp_09677ae292e4f07d016ae02fbf146c81918fe9fc903210d00f |
| medium | 0 | TERRA-OK | gpt-6.1-terra / medium | 01a17bba-fd62-7110-9bd2-80e2ded0477a | resp_05c453190d18ec00016ae02fe412e48191be350d2f5ac5716f |
| high | 0 | TERRA-OK | gpt-6.1-terra / high | 01a17bba-fd79-7600-a166-b3aee40802c8 | resp_0c70ff425bf227c8016ae02fe8e6008191a5a375ce17c8b667 |
| xhigh | 0 | TERRA-OK | gpt-6.1-terra / xhigh | 01a17bba-fd7e-7ec3-bd1e-4a55723d0095 | resp_0e85bcdae8e173f1016ae02fe42a088191a99c46439e3cc9ca |
| max | 0 | TERRA-OK | gpt-6.1-terra / max | 01a17bba-fd77-7762-b849-f85f653f512e | resp_0ea6865d9f217bf8016ae02fe37e408191a6ab6d43a056cdda |
| none | 0 | TERRA-OK | gpt-6.1-terra / none | 01a17bba-fd84-7570-905d-0c6072c75ed1 | resp_06411cc25182f082016ae02fe3db2c81919f27ba55f632ded7 |

Codex accepted `none` although its model list does not advertise it. The receipt does not show that `none` maps to AA's non-reasoning variant.

## Why Terra stays discovery-eligible

These smoke runs used a read-only sandbox, not Office's worker launch profile, and do not write a `route_probes` record. Office qualifies a discovery-eligible row through its own exact per-fingerprint probe (`office/route_probe.py`), which this PR does not bypass. So all six Terra rows keep `dispatchable: false`, `discovery: eligible`. 6.1 Sol rows are unchanged and gated the same way. While a 6.1 row is non-dispatchable it can run only as a discovery trial (with 5.6 as the trial's fallback), and normal automatic routing keeps selecting 5.6. Supersession takes effect once a 6.1 row is dispatchable and its route clears trust and the other gates.
