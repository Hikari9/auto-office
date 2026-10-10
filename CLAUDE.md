# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Auto Office is an agent engineering lifecycle (intent → plan → routed execution → independent review → verification → landing) behind one `office` CLI. Distribution `auto-office`, Python package `office` in `src/office/`, entry point `office.cli:main`. Version lives in both `VERSION` and `pyproject.toml`.

## Commands

```bash
uv tool install --force --editable '.[visual]'     # puts `office` on PATH from this checkout
scripts/validate.sh                                # first run also creates .venv with test deps

.venv/bin/python -m pytest -n 2                    # default: in-process unit smoke tier
.venv/bin/python -m pytest -n 2 --all              # unit + integration + legacy + slow
.venv/bin/python -m pytest -n 2 -m integration     # or -m legacy / -m slow
.venv/bin/python -m pytest tests/test_amendments.py -k some_name   # single test

python3 scripts/check_ecosystem.py                 # static ecosystem/contract check
scripts/validate.sh                                # full local gate (the pre-push hook runs this too)
VALIDATE_BUILD=1 scripts/validate.sh               # also builds the wheel and checks bundled resources
git config core.hooksPath .githooks                # enable the pre-push gate
```

- Cap pytest workers at 2 (`-n 2`); the gate does the same.
- There is no remote CI. `scripts/validate.sh` is the only gate and stamps the validated tree under `$(git rev-parse --git-common-dir)/office-validated` so pre-push can skip a rerun.
- Markers (`pyproject.toml`): `integration`, `legacy`, `slow` are deselected by default. `approved` starts from an approved-plan snapshot. `review_contract(name)` pins the run's review contract (v3.1 suites test pinned pre-#337 runs).
- Tests use isolated homes and scripted fake harness binaries. Never call a real model from a test.
- Property tests use Hypothesis (`from hypothesis import given`). Use one where an invariant over generated inputs says more than a table of examples, not to convert ordinary example tests. `tests/conftest.py` loads a deterministic profile (`derandomize`, no example database); `HYPOTHESIS_PROFILE=explore` randomises and widens a one-off hunt. Keep a shrunk counterexample as an `@example(...)`. Keep a named example where the example itself is the contract or the historical regression.
- Mutation testing (mutmut) audits the core-logic tests by hand and is not part of the gate: `docs/mutation-testing.md`. Install with `.[mutation]`.
- To dogfood a change: `uv tool install --force --reinstall --no-cache "auto-office[visual] @ <checkout>"` then `office install`.

## Architecture

The module map is in `docs/v31-implementation.md` §1. The big-picture rules:

- **`runs.db` (SQLite, WAL) is the only lifecycle authority.** Every semantic transition runs inside `db.transaction` (`BEGIN IMMEDIATE`). Files under a run directory (`view.json`, evidence) are generated views; editing them changes nothing. `.office/active/` and `.office/sessions/` are stat-only indexes for the hook fast path.
- **Outbox, no daemon.** A transition needing outside work (launch agent, run checks, review, capture, integrate) inserts an `outbox` row in the same transaction. `jobs.kick` spawns short-lived `office _job <id>` processes that claim the row (flock + `claim_token`), work, and record the result in a second transaction. Dead claims are reclaimed by the next `office` command. A gate takes the first terminal result only.
- **Version pinning.** Each run pins to a runtime version (`frontdoor.py`, `runtime_default.py`, `version.py`). 3.0 runs route through the retained legacy runtime (`legacy.py`). `scripts/` is the retained 3.0 helper surface, reachable via `office raw` (`compat.py`), and cannot write a 3.1+ run. Schema changes to v3 recorder tables add only nullable columns so the old runtime keeps working on the same DB.
- **Review contracts.** New runs default to `convergence-v1` (`convergence.py`, `docs/review-convergence.md`). Runs pinned to `v3.1` keep rolling plan-review semantics (`docs/v31-rolling-review-gates.md`). Changes to review behavior must preserve both.
- **Flow through modules:** `lifecycle.py` (start/resume/close) → `plans.py`/`planfile.py` (PLAN.md, plan review) → `authority.py` (user approval) → `dispatch.py` (leases, worktrees, agent launch) → `submit.py` (immutable revisions) → `gates.py` (checks, review, acceptance) → `integration.py` → `land.py`/`closeout.py`. `amend.py` handles amendments. `guide.py` produces `status` and its `next:` line.
- **Routing** (`routing.py`, `scoring.py`, `adaptive.py`, `candidates.py`, `benchmarks.py`) picks harness/model/effort per role under trust and capability floors, using `catalog/`, `config/`, and harness adapters in `adapters/` (validated by `scripts/office_runtime.py validate-adapter`).
- **Packaged resources.** `config/`, `schemas/`, `catalog/`, `adapters/`, `skills/`, `SKILL.md`, `VERSION`, and the `scripts/*-usage.py` probes ship inside the wheel as `office/_resources/`. Adding a runtime-read file outside these needs a `pyproject.toml` force-include entry.
- **Skills.** `SKILL.md` is the orchestrator operating contract. `skills/` holds role skills (`auto-planning`, `auto-review`, harness CLIs, etc.). When runtime behavior changes, update the skills that describe it.

## Terminology

`CONTEXT.md` defines project terms (plan defect, brief defect, ordinary vs contract amendment, requirements change, authorization). Use those terms, and avoid the listed `_Avoid_` synonyms, in code, docs, and findings. Design rationale is in `MANIFESTO.md` and `references/OFFICE-SKILLS-V3-*.md`.
