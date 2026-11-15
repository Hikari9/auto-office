# Hydrogen shadow-review lane (#1168)

This lane tests whether `agy/gemini-4-hydrogen` reviews code as well as Luna (`codex/gpt-6-luna@high`). It is out of band. Luna's Office review is the only gate. Hydrogen's verdict never blocks, approves or reroutes anything. Office still vetoes Hydrogen for every reviewer role (`hydrogen_trials.route_refusal`), and nothing here changes that.

The AA v5 shadow lane from #862/#1144 (`eval/aa_v5_*`) replays executor calibration against benchmark scores. It has no reviewer launches or review findings, so this lane is separate.

## Files

- `planted/<case>/change.diff` and `truth.yaml`: five realistic patches against real Office code at `planted/BASE`, one seeded defect each (logic, auth, off-by-one, missing test, race). `make_planted.py` regenerates the diffs from a clean checkout of that base.
- `shadow_review.py`: runs one reviewer, read-only, in a detached scratch worktree, with one shared prompt. It writes `results/<case>/<reviewer>.json` and removes the worktree afterwards. Luna runs through `codex exec -s read-only`. Hydrogen runs through `agy --mode plan` at `gemini-4-hydrogen-high`.
- `scorecard.py`: computes per-reviewer planted recall, blocker agreement with Luna, false-positive rate and escapes, and writes `SCORECARD.md`.
- `dispositions.yaml`: the orchestrator's accept or reject for each finding, keyed `case -> reviewer -> finding index`.
- `escapes.yaml`: post-merge defects traced to a shadow-reviewed PR, with the reviewers that missed them.

## Per live PR (the next ~10 Office PRs)

1. Let Office run Luna's review as usual and land on Luna's verdict alone.
2. Then run both reviewers through this lane on the same PR so they see the same prompt:

   ```bash
   python3 research/hydrogen-shadow/shadow_review.py --pr <N> --reviewer luna
   python3 research/hydrogen-shadow/shadow_review.py --pr <N> --reviewer hydrogen
   ```

   The shadow Luna run is the comparison baseline. Office's gated Luna review stays the authority.
3. Under `pr-<N>` in `dispositions.yaml`, mark every finding from both reviewers `accept` (a real defect, even if it was not planted) or `reject` (not a real defect).
4. If a defect later surfaces in code a shadow-reviewed PR changed, append it to `escapes.yaml` with `missed_by`.
5. Run `.venv/bin/python research/hydrogen-shadow/scorecard.py` and commit the results, the dispositions and `SCORECARD.md`.

A run that fails (quota, timeout or an unparsed reply) is recorded with its status and left out of the metrics. Re-run it once quota resets.

## Metrics

- **Planted recall:** planted cases where at least one finding matches the truth. A finding matches when its file is the same and its line is within ±5, or when its summary names a truth keyword. Check the listed matches by hand.
- **Blocker agreement with Luna:** cases where both reviewers raise a blocker or major finding in the same file within ±10 lines, or neither raises one.
- **False-positive rate:** rejected findings divided by all findings. A finding that matches a planted truth counts as accepted unless a disposition overrides it.
- **Escapes:** entries in `escapes.yaml` that name the reviewer in `missed_by`.

## Promotion rule

Promote Hydrogen only if, over the planted set plus at least 10 live PRs with every finding dispositioned:

- its planted recall is at least Luna's,
- its escapes are no more than Luna's, and
- its false-positive rate is strictly lower than Luna's.

Anything short of that keeps Hydrogen out of reviewer routing. Promotion would also need a separate change to the Hydrogen reviewer veto, with its own review.
