# Implementation notes

## Normative vs implementation data

`OFFICE-SKILLS-V3-SPEC.md` is normative. Code/config in this bundle is an implementation candidate.

The implementation intentionally does **not** invent provider facts the spec expects to be refreshed or proven locally. In particular:

- real benchmark values and current prices are absent until imported;
- model release dates are unknown unless supplied by a source snapshot;
- quota probes may return `unknown` rather than pretend quota is unlimited;
- seed real-harness adapters remain `valid-unverified` until deterministic/live conformance and runtime evidence satisfy policy;
- no seed adapter receives normal mutable executor, final review, destructive, or production-browser authority merely because its CLI shape is known.

## Compatibility seed: five frozen fields

The uploaded spec refers to five frozen execution fields but does not enumerate their names. The current Auto Office implementation uses:

1. `goal`
2. `done_criteria`
3. `blast_radius`
4. `named_actions`
5. `non_goals`

This preview keeps those names to make migration concrete. Treat them as compatibility data, not a new normative requirement.

## Route helper contract

`office_runtime.py route` accepts a JSON/YAML request containing `role`, `gear`, `playbook`, `policy`, and `candidates`. Candidates already represent locally discovered/bound `harness@version × model × effort` triples. The helper performs deterministic filtering and selection; it does not use the network.

Successful route results include a `selection_disclosure` with the canonical model identity, the exact harness invocation identifier when available, and an evidence-based reason. Dispatch telemetry persists the invocation identifier and reason so the user-visible notice and durable record agree.

## Catalog refresh boundary

`catalog-snapshot` creates a content-addressed immutable normalized snapshot from a local file. Fetching provider/benchmark source data is intentionally kept outside route-time; an agent or scheduled maintenance step may fetch source data and then pass the normalized input to this command.

## Proposal isolation

The scripts can compute deterministic proposal identities and privacy-lint generated material. Git worktree/branch/PR creation remains an orchestration action because repository providers and credentials are environment-specific. The skill requires those actions to occur away from the pinned runtime worktree and never activates an unmerged proposal into the current run.
