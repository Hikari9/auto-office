# Harness adapter contract

A valid adapter declares id, verification state, version fingerprint, model source, effort mapping, benchmark slug mapping, invocation, safe prompt passing, dispatch forms, trusted evidence capabilities, quota probe, shallow review, agentic/builder evidence, failure signatures, and conformance state.

Trust states:

- `invalid`: deterministic contract failure; never routable.
- `valid-unverified`: schema/conformance shape passes but lacks runtime evidence. Default use is discovery, read-only investigation, prototypes, reversible sandbox workers, and conformance. No normal mutable executor, final code review, destructive action, or production-facing browser verification unless user explicitly overrides. This is also the floor state absent any explicit trust act, regardless of dispatch history.
- `proven`: reached only through an explicit recorded trust act (below); never computed from a dispatch count or any query over `outcome_labels`.

**Trust moves down automatically, never up (amendment v5, made executable by v6).** A query derives quarantine from recorded evidence with no human action: any unresolved adapter-attributed critical failure (`recurrence_failure`, `material_post_merge_defect`, or an `abandoned` dispatch carrying an accepted-material critical/high finding) quarantines the triple, and failure evidence latches — a later benign self-reported label never retracts it, and no query clears a standing quarantine. The only way trust ever rises — `quarantined` → `valid-unverified`, or anything → `proven` — is an explicit `adapter_trust_acts` record carrying its own `actor_id`, a substantive `reason`, and never inferred/backfilled from evidence. `adapter_trust.proven_min_successful_dispatches` (default 5) and `proven_min_task_shapes` (default 2) remain in `config/config.default.yaml` as advisory reference numbers an actor may consult before recording a `proven` act; no query binds them, and meeting them has no automatic effect — that is correct, not an unimplemented gap. `tests/test_trust_conformance.py` is the normative artifact for this invariant, not any SQL example in a document. See `docs/v3-runtime-contracts.md` §7.1 and `protocol/routing.md`.

Installed harnesses are detected locally through PATH and adapter fingerprints. Do not scrape arbitrary package registries for harness discovery. A catalog model that local harness support cannot prove is `discovered-unconfirmed` and cannot receive normal mutable routing until adapter update, safe probe, explicit user selection, or qualifying sandbox/reversible evidence.
