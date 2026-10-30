# Artificial Analysis benchmarks: Xiaomi MiMo-V2.6 (added 2026-10-09)

Snapshot taken 2026-10-09 from artificialanalysis.ai per-model pages
(`/models/mimo-v2-6-pro`, `/models/mimo-v2-6-flash`). Intelligence scores are
**Artificial Analysis Intelligence Index v4.3.2** (the version both pages publish) and are
not comparable with other index versions. See `catalog/seed.yaml` `benchmark_index_notes`.

**No effort tier.** AA publishes one entry per model — no `(low|medium|high|...)` variant —
and both records fix `reasoningTokens: 2000`. The score is therefore the model score, not an
effort-specific measurement, and the catalog rows say so in `metrics_context`. The office
effort on each row (`medium`) names the `pi --thinking` level the row was locally dispatched
at; it does not claim AA measured that level.

Columns follow `aa-intelligence-v4.3.2-2026-09-28.md`: **Intel.** is the AA index (integer as
published on the page, raw value in parentheses), **TB 4.0** Terminal-Bench 4.0, **SciCode**
percent, **Coding** the mean of the two computed here, **$ in / $ out** list price per 1M
tokens, **$/task** AA's weighted cost per Intelligence Index task, **Tok/s** median output
speed, **TTFT** time to first token.

| Model | Intel. | TB 4.0 | SciCode | Coding | $ in /1M | $ out /1M | $/task | Tok/s | TTFT | Released |
|---|---|---|---|---|---|---|---|---|---|---|
| MiMo-V2.6-Pro | 46 (46.32) | 34.8 | 60.9 | 47.9 | $0.43 | $0.87 | $0.13 | 42.5 | 4.11s | 2026-09-21 |
| MiMo-V2.6-Flash | 38 (37.88) | 22.7 | 51.3 | 37.0 | $0.14 | $0.28 | $0.06 | 57.5 | 3.37s | 2026-09-21 |

Both models are open weights (Xiaomi, MIT, 1M-token context).

## Local harness evidence (the invocation side, not the benchmark side)

The two rows are routable because `pi` 1.1.0 reaches these slugs locally, not because AA
lists them. Recorded 2026-10-09 on this machine:

- `pi --offline --list-models` lists `mimo-v2.6-flash` and `mimo-v2.6-pro` under provider
  `xiaomi-token-plan-sgp`, so the rows pin that provider in `invocation_model_id`
  (`xiaomi-token-plan-sgp/mimo-v2.6-pro`, pi's documented `provider/id` syntax) and never
  resolve an ambiguous global default.
- `pi auth check --model xiaomi-token-plan-sgp/<model>` answers `ready` (exit 0) for both;
  the adapter's `preflight` runs this and the model list before every headless launch and
  stops it (`preflight_failed`) when the pinned slug is not listed here or credentials are
  missing — a local probe at registration time proves nothing about another host.
- `printf <prompt> | pi --print --no-approve --no-extensions --no-mcp --model xiaomi-token-plan-sgp/<model> --thinking medium --no-session`
  answered the stdin prompt, created and ran `hello.py` in a throwaway git repo, and
  exited 0 for both slugs. A project `AGENTS.md` instruction still applied under these
  flags (context files are not project-trust gated), while `.pi/` resources (settings,
  `mcp.json`, extensions, skills) are declined: `--approve` is project-resource trust,
  not a sandbox or a `bypassPermissions` analogue, and an Office worktree is untrusted
  input.
- Failure behavior (same flags): an unknown provider exits 1 (`Unknown provider ...`);
  an unlisted model under the pinned provider is taken as pi's "custom model id" and the
  provider API returned 400 (`Unsupported model`), exit 1. A provider that silently
  serves a custom id as a different model would still yield exit 0 wrong-model output —
  that is the declared failure signature `custom-model-id-served-as-something-else`, and
  exit 0 is therefore launch evidence, **not** model conformance (no model identity is
  read back yet).

Only `medium` is proven to launch. No `--thinking` level is proven to change model behavior,
so `effort_confidence` is `mapped`, and the exact-effort conformance that would let another
effort row be added is still pending.
