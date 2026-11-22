# v2 to v3 migration

1. Keep in-flight runs on the exact skill/policy snapshot they already loaded.
2. Introduce the canonical `auto-office` lifecycle and v3 durable schemas first.
3. Migrate unique evals from branded offices into the shared `evals/` suite or explicitly retire them with rationale.
4. Convert harness-specific knowledge into adapter data and primitive harness skills; do not preserve Codex/Claude/Agy as separate lifecycle implementations.
5. Migrate legacy config reads to `.auto-office/config.yaml`, `~/.config/auto-office/config.yaml`, and `config/config.default.yaml` precedence.
6. Treat existing prose lessons as qualitative low-weight priors or concrete hard exclusions only where incident evidence exists; never synthesize fake telemetry.
7. Start structured v3 telemetry under a new schema version.
8. Publish compatibility notes, then remove branded lifecycle copies after the eval migration is complete.
9. Do not promise compatibility after a reload into v3.

The migration should follow the v3 implementation sequence: durable state and policy pinning before routing/learning, recorder before local-evidence routing, and self-healing last.
