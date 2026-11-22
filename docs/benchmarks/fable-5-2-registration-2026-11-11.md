# Claude Fable 5.2 — gated catalog registration (2026-11-11)

## Scope and authority
Register Claude Fable 5.2 **as a documented, non-dispatchable Claude-harness model**. This is model onboarding, not an Auto Office benchmark-refresh submission, benchmark-index migration, a trust grant, or permission to route production work. The model was released November 9, 2026. Anthropic's exact API model ID is `claude-fable-5-2`, but successful selection of that ID and each effort level by the locally installed **Claude Code CLI** has not been observed.

The existing authoritative catalog is split across `catalog/routes.tsv`, `catalog/benchmarks.tsv`, `catalog/provenance.yaml`, and `catalog/manifest.yaml`; the packaged `plugin-dist/catalog/` mirror must be kept identical. There is **no** active `catalog/seed.yaml`.

## Vendor facts
- Anthropic release: https://www.anthropic.com/claude-fable-and-mythos-5-2
- Official model ID, date, 1M-token context, 128K-token maximum output, always-on adaptive thinking, default effort `high`, and $8 per million input / $40 per million output tokens: https://platform.claude.com/docs/en/models/fable-5-2/overview
- All five API effort levels `low`, `medium`, `high`, `xhigh`, and `max` are supported: https://platform.claude.com/docs/en/build-with-claude/effort
- The $8/$40 prices are ordinary API list rates, **not** proven actual Claude subscription quota/cost. Fast mode and caching have separate price rules. This model requires 30-day data retention unless Anthropic grants an exception; consider that before testing sensitive tasks.

## Published AA scores — reference only, not imported

The currently published **Artificial Analysis Intelligence Index v5.0.1** includes these five *Default Fallback* variants:

| API effort | AA v5.0.1 score | Shipped v4.3.2 score |
| --- | ---: | --- |
| low | 54 | null |
| medium | 56 | null |
| high | 62 | null |
| xhigh | 62 | null |
| max | 65 | null |

AA source: https://artificialanalysis.ai/models/releases/claude-fable-5-2 and https://artificialanalysis.ai/models/releases/comparisons/claude-fable-5-2-vs-k2-v2

**Do not copy these numbers into `catalog/benchmarks.tsv` as v4.3.2 values.** The shipping index in `catalog/manifest.yaml` is **Artificial Analysis Intelligence Index v4.3.2**, while these Fable figures are **v5.0.1**, and the index definitions differ. The five new rows explicitly store `\\N` under the pinned v4.3.2 index. AA fallback semantics are also not proof of how a local Claude CLI request runs.

Migration discussion and benchmark-to-local-outcome calibration are deferred in https://github.com/Hikari9/auto-office/issues/862. This PR neither approves nor implements that migration.

## Activation prerequisites (not completed in this PR)
1. On the actual host/account, verify `claude --version` and that `claude --model claude-fable-5-2 --effort <level>` is accepted for each intended tier. Do not infer CLI support from Anthropic's API documentation or an exit-0 banner.
2. Capture **actual selected model and effort readback** (session transcript), including controls with a different account default, so silent fallback, inherited settings, and mislabeling cannot pass.
3. Run the repo's full `office doctor --probe-route` / model-conformance, quota, permission, write boundary, and independent review checks before proposing any dispatchable or trust-state change. Discovery eligibility is not route qualification.
4. Test model-specific behaviors before live work: always-on adaptive thinking; classifier-triggered fallback/refusal; 30-day retention; transcript/tool compatibility; long-horizon cost and quota.
5. If a future proposal uses AA v5.0.1 as an active route prior, seek a **separate explicit index-migration decision** with historical snapshot/pinned-run safeguards.

No alias, floor, policy, preferred seed, trust baseline, adapter argv, invocation source of other rows, or existing score is changed.
