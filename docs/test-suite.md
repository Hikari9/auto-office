# The test suite: what it is for and how it was pruned (#496)

The suite's job is to fail when Auto Office's promised behaviour breaks, and to stay green when only the
implementation changes. Two questions decide whether a test earns its place:

1. If the implementation changed and the promised behaviour did not, should this test still pass?
2. If the promised behaviour broke, would this test fail?

Be suspicious of a test whose expected value is computed with the same helper, branch structure or table as
the code it checks. It cannot fail for a reason the code does not share.

## Tooling

- **Hypothesis** states a behaviour as a property over generated inputs. Use it where an invariant says more
  than a table: combinatorial policy (routing, scheduling, readiness), parsers and path rules, migrations. Do
  not use it to rewrite an ordinary example test. `tests/conftest.py` loads a deterministic profile
  (`derandomize`, no example database, no deadline), so a laptop and the pre-push gate see the same examples.
  `HYPOTHESIS_PROFILE=explore` randomises and widens the search for a one-off hunt. A shrunk counterexample
  worth keeping becomes an `@example(...)` beside the property; so does each boundary that generation would
  otherwise reach only by luck.
- **mutmut** audits whether the tests notice a changed behaviour (`docs/mutation-testing.md`). It is evidence,
  never a target: a survivor is read, and a test is added only for behaviour someone relies on.
- Write generators so most draws are meaningful. A property filtered by `assume` fails Hypothesis's health
  check when the filter rejects too much; build the valid input instead.

## What stays as examples

A named example is kept when the example is the contract or the history:

- a regression for a real bug, named by its issue or review (`#405`, `#420`, `#446 F1`, `M7`, `ROUND ²`);
- a safety or authorization boundary (authority terms, request-scope paths, undeclared command targets);
- a documentary table that a reader consults (which file names count as tests, which sed forms are rewritten);
- an end-to-end case that shows the pieces are wired together, even when a property covers the rule.

Mock-choreography tests are rare here: 3 uses of `mock` in the whole suite, and the herdr and harness tests
drive a fake binary at the real command-line boundary, which is the contract. The audit found little to
rewrite there.

## Audit

All 164 test files (2,920 collected cases) were read by theme, with the parametrized matrices, duplicated
bodies and private-API use mined by script. Findings, by class:

| Class | Where | Action |
| --- | --- | --- |
| Example matrix over a policy | self-review tier, risk floor presets, scheduler admission, orchestrator fallback | Property replaces the matrix |
| Policy with no matrix, only a few examples | route qualification, rejection stage, quota reserve, advisory anchor, money band, capability floor, preferred seed, reward tie-break | Properties added beside the examples (nothing deleted) |
| Example matrix over a parser or path rule | ledger lines, test-file rule, request-scope paths, command target shapes | Property plus the named regressions |
| Matrix over a generated state space | schema migration (111 cases, one per column) | Property over column subsets and recorded versions |
| Expected value computed like production | `_expected_tier` in the tier table | Removed; properties state the safety rule |
| Strict duplicate | stale-office refusal (13 cases, a subset of the stronger test in `test_api_commands`), `test_task_must_belong_to_the_run` | Deleted |
| Slow end-to-end case repeated per input | fix-round tier, request-scope refusal, dependency readiness | One or two end-to-end cases plus a unit property |
| Assertion weaker than its name | malformed ledger lines accepted any later error with the same line number | Strengthened: the parser must reject it |
| Mock choreography | none worth changing | Kept |
| Implementation-detail (private API) | `_usage_limit`, `_herdr_pane` and similar | Kept: real transcripts and a fake binary, behaviour at the boundary |

## What changed

No production code changed. Every change is under `tests/`, `docs/`, `pyproject.toml` and `.gitignore`.

### Deleted, signal already held elsewhere

- `test_every_kind_is_refused_while_office_is_stale` (13): a strict subset of `test_stale_office_refuses_every_mutation`,
  which also asserts nothing was launched, sent or recorded.
- `web/test_target_validation.py::test_task_must_belong_to_the_run` (1): a strict subset of the same-named test in
  `test_api_commands.py`.

### Consolidated into properties

| Replaced | Cases | By |
| --- | --- | --- |
| tier table, irreversible/size/high, unparsable risk, unknown gear, unrecognized blast, full gear | 143 | 5 properties on `self_review_tier` (inline only when all clear, clean low gear is inline, any high signal is deep, gear `full` is deep, unusable record is single) plus one through `resolve_risk`, with an `@example` per single high signal; the `_expected_tier` oracle is gone |
| fix-round tier per gear/blast | 4 integration | 1 unit property over generated gear and risk |
| risk floor under every preset | 12 | 1 property over the config's own presets and generated risk |
| schema migration, one case per shared column, plus "newer version not lowered" | 112 | 1 property over column subsets and recorded versions |
| commit sha lengths | 6 | 1 property over every length, 1 retained prefix example |
| non-code files and package markers in the test-file table | 10 | 3 properties |
| `ready` per dependency status | 6 integration | 2 properties over generated dependency graphs and statuses |
| request-scope path refusals | 5 integration | 7 unit properties |
| undeclared, missing, mistyped command targets | 12 | 3 properties over every kind |
| orchestrator fallback table | 7 | 1 property over primaries, quotas, fallbacks, installed set and reserve |
| measured pressure, paused auto mode | 3 | admission properties and `test_a_gated_item_names_what_gates_it` |

### Rewritten, same cases

- The 40 malformed ledger lines now run through `parse_ledger` and `check_ledger` with no git repository, and assert the
  parser rejected the line (they ran through a repo and accepted any later fix line numbered the same, which let
  `fixed a b c` pass for a different reason). One case still goes through the whole verdict.
- The 33 authority-term examples are two tests instead of four, all examples kept (review #446 F1, M7).

### Added

65 property tests (`@given`) across 12 files, listed in the pull request. New coverage: route qualification, rejection stage,
quota reserve, advisory anchor and money band (`test_routing_properties.py`); the capability floor, reward tie-break,
harness major and preferred seed for both the packaged and the retained 3.0 scorer; admission capacity (exactly full, never over),
ready order, and reserve handling in the scheduler; well-formed ledgers round-trip; and boundary examples the mutation run showed
generation alone missed.

## Intentionally kept despite overlap

- Both `test_queue_issue_refuses_a_duplicate_queue_item` tests: one checks the refusal after a real queue add, the other that
  a different issue is not blocked by the existing item.
- The 15 `portable_sed` cases and the quoting cases of the shell guard: each is a distinct bug.
- The remaining target-shape rows (an issue is not a run, `pane` is never accepted, `repo` over 512 characters).
- The two dependency end-to-end cases (`submitted` is the #405 regression; `accepted` is the opening).
- The two end-to-end request-scope refusals (`/etc/passwd`, `../outside`): they show the CLI keeps the task running and records nothing.
- Everything in `legacy/` and `slow`: not touched.

## Mutation evidence

See the table in the pull request description; the method and how to rerun it are in `docs/mutation-testing.md`.
