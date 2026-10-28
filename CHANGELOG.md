# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

## 3.10.3

- fix(validation): baseline-classify integration failures, diagnose tracked check mutations and fence superseded plan reviews (#629).

## 3.10.2

- fix(routing): honor harness-major user rules for declared routes and register Argon lineage; align release tests with current routing and catalog policy.

## 3.10.1

- test(gate): run changed-file-targeted `--all` tests for each PR and pre-push; attest scoped Office landing checks separately from the explicit full suite (#854).

## 3.10.0

- feat(catalog): move the shipped model seed to strict, normalized TSV routing and benchmark tables plus YAML provenance; keep legacy in-memory objects, run hashes, lineages, user overlays and policy unchanged (#852).

## 3.9.0

- feat(routing): a user role allow (or exact `preferred_seed` entry) makes a route dispatchable with no proof, qualification or trust gate and overrides a catalog or user-overlay `dispatchable: false`; a runtime route failure is logged (`route.failed`), demoted for a TTL and falls back to the next eligible route; `office route policy --effective` shows what dispatches and why, and `office route allow a,b` reshuffles several routes at once (#845).
- feat(routing): committed role policy is size-scoped allow / neutral / deny from one data file, `catalog/role-policy.yaml` (#850 sections 1-3). Executor follows the #809 slate b¶»§q«^