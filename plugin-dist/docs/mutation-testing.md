# Mutation testing the core-logic tests

Mutation testing answers a question the pass/fail gate cannot: does the suite notice when behaviour
changes? [mutmut](https://mutmut.readthedocs.io/) makes small edits to a module (flip a comparison, drop a
branch, change a constant), reruns the tests that reach the edited function, and reports whether any test
failed. A mutant nothing notices is a place where the suite would pass a real regression.

It is an audit, not a gate. `scripts/validate.sh` does not run it, and a score is never a target. Survivors
are read one at a time, and most are equivalent edits or cosmetic text. Write a test only for a survivor that
is a behaviour someone relies on.

## Scope

`[tool.mutmut]` in `pyproject.toml` names the modules, all deterministic policy with no subprocess or
browser in the way:

| Module | Functions audited | Tests that exercise them |
| --- | --- | --- |
| `scoring` | capability floor, reward tie-break, harness major, triple normalisation | `test_scoring_properties`, `test_trust_major`, `test_intelligence_floor`, `test_sonnet_5_5`, `test_haiku_5_5` |
| `routing` | `route`, `candidate_id`, `preferred_rank` | `test_routing_properties`, `test_adaptive_routing`, `test_quota_routing`, `test_task_aware_routing`, `test_alias_resolution` |
| `scheduler` | scoring, ready order, admission | `test_scheduler` |
| `guide` | dependency readiness | `test_dependency_readiness` |
| `briefs` | `self_review_tier` | `test_preflight` |
| `preflight` | ledger parsing and checking, test-path rule | `test_self_review_ledger`, `test_preflight` |
| `config` | `resolve_risk`, `resolve_gates` | `test_risk_floor_lightweight`, `test_config_command` |
| `web/service` | `check_target`, `orchestrator_route` | `test_target_validation`, `test_launcher`, `test_api_commands`, `test_settings_api` |

Deliberately out of scope: browser, dogfood, herdr, and any test that starts `office` in a child process. A
child process imports the mutated tree without mutmut's bookkeeping and fails for reasons unrelated to the
mutant. Integration tests (the `env` fixture) are left out too: a few of them stop returning under mutmut's
stats pass even though they pass normally. Only the unit tier is measured, so a behaviour covered only by an
integration test shows up here as untested.

## Run it

```bash
uv pip install --python .venv/bin/python3 -e '.[test,mutation]'
export OFFICE_PINNED_LEGACY=1
# One module at a time: narrow the test selection to its files, name its functions.
.venv/bin/mutmut run 'office.scoring.x_evaluate_capability_floor*' 'office.scoring.x_reward_sort_key*'
.venv/bin/python tests/tools/mutation_summary.py 'office.scoring'              # per-function table
.venv/bin/python tests/tools/mutation_summary.py --survivors 'office.scoring'  # plus every survivor's diff
.venv/bin/mutmut show office.scoring.x_harness_major__mutmut_4                 # one mutant
```

- Name the functions. `mutmut run` with no names runs every mutant in scope and takes hours.
- `pytest_add_cli_args_test_selection` in `pyproject.toml` is the default selection (the unit tier). Running
  every selected file against one function wastes time and, for some integration tests, hangs; narrow the
  selection to the module's tests, as in the table above, by editing it for the run.
- mutmut copies the repository into `mutants/` (git-ignored) and runs the tests there. The first step,
  "Running stats", runs the selection once to learn which tests reach which function.
- `max_children = 2` matches the repository's two-worker limit.
- Statuses: `killed` (a test failed, good), `survived` (nothing failed), `no tests` (no selected test reaches
  the function), `timeout`, `suspicious`.

## Reading a survivor

1. **Equivalent**: the edit changes nothing observable, for example `return (1, 0.0)` becoming
   `return (1, 1.0)` in a sort key whose only use is ordering. Leave it.
2. **Cosmetic**: only the wording of a message changed. Leave it unless the wording is a contract.
3. **Real gap**: a branch, boundary or default no test pins. Add the smallest test that states the behaviour
   as a property or a named example, then rerun that function.

Results for the #496 consolidation, before and after, are in `docs/test-suite.md`.
