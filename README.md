# Auto Office v3 ecosystem preview

This bundle is a reference implementation of the supplied **office-skills v3 — Adaptive Office Runtime Specification**. It is intentionally structured as one canonical `auto-office` lifecycle with internal protocol/spoke skills and harness primitives, rather than branded lifecycle copies.

## What is included

- `SKILL.md` — canonical Auto Office control plane.
- `skills/auto-*` — narrow lifecycle spokes for planning, routing, execution, review, verification, closeout, maintenance, adapters, and self-improvement.
- `skills/codex-cli`, `skills/claude-cli`, `skills/agy-cli` — directly invocable harness primitives. They are mechanics, not separate offices.
- `schemas/` — machine-checkable run envelope, execution packet, adapter, quota, finding, outcome, and routing-candidate contracts.
- `config/config.default.yaml` — v3 policy defaults grounded in the spec.
- `catalog/seed.yaml` — cold-start identity rows for the planner/reviewer names explicitly present in the spec. Benchmark/price fields remain unknown rather than invented.
- `adapters/seed/` — conservative Codex/Claude/Agy seed adapters marked `valid-unverified`; none is falsely shipped as `proven`.
- `scripts/office_runtime.py` — deterministic helper CLI for validation, routing, snapshots, SQLite recording, maturity, replay, privacy lint, catalog snapshots, adapter scaffolding, and proposal identity.
- `evals/` and `tests/` — acceptance scenarios and executable unit tests.
- `references/OFFICE-SKILLS-V3-SPEC.md` — exact uploaded normative spec.

## Preview caveats

The v3 spec deliberately leaves several provider-specific facts to refreshed catalog/adapter data. This bundle does the same. Real benchmark indexes, prices, current model slugs, provider quota APIs, and live harness conformance are **not fabricated**. Seed harness adapters are therefore `valid-unverified` and must pass deterministic + live conformance and the configured runtime evidence bar before normal mutable routing.

The uploaded spec references “the five frozen execution fields” without naming them. To preserve continuity with the current `office-skills` implementation, this preview seeds `goal`, `done_criteria`, `blast_radius`, `named_actions`, and `non_goals`. That mapping is called out as compatibility data and should be replaced if the ratified v3 packet contract defines different names.

## Quick smoke test

```bash
python3 scripts/check_ecosystem.py
python3 -m unittest discover -s tests -v
python3 scripts/office_runtime.py init-db --db /tmp/auto-office-runs.db
python3 scripts/office_runtime.py maturity --points 60
```

## Installation shape

The ZIP root is `auto-office/`, matching the existing plugin convention. It contains ChatGPT `agents/openai.yaml` metadata as well as `.claude-plugin/plugin.json` for the current Claude plugin layout.
