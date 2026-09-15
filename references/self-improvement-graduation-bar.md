# Self-improvement graduation bar

What a proposal on the standing branch must satisfy before a maintainer merges it. The bar is
here rather than in a script because the last condition is a human decision; the rest are
checkable, and `scripts/office_propose.py` enforces the two that are cheap to get wrong.

A proposal is **eligible** when all of the following hold.

1. **Privacy.** `office_runtime.py privacy-lint` reports `valid: true` on the public text. The
   appender checks this itself and refuses to write a failing proposal, so an ineligible-on-privacy
   proposal never reaches the branch at all.
2. **Opaque evidence.** Every evidence reference is a hash. No raw transcript, no repo or person
   name, no absolute path, no issue prose. Source counts are public; sources are not.
3. **Lineage without exposure.** The proposal carries a `lineage_digest` that the originating
   machine can reproduce from a run id it already holds, and that nobody else can invert.
4. **Support threshold.** Each pattern cites at least two independent evidence rows. One
   occurrence is an incident, not a pattern; a proposal built on a single row is a report and
   belongs in the run's own findings instead.
5. **Stream discipline.** A `learned-pattern` proposal does not change capability floors, reward
   definitions, hard exclusions, destructive permissions, maturity policy, or security boundaries.
   A proposal that would is a `catalog-policy` proposal and additionally needs replay output, eval
   results, a diff explanation, and the resulting policy hash change.
6. **Independent review.** Reviewed by an agent that did not produce it, with the finding recorded
   as `accepted-material`, `accepted-minor`, or `rejected-on-evidence`. Self-review is a pass,
   never an approval.
7. **Idempotent identity.** Appending the same dream again is a no-op. A branch that grew a second
   copy of a proposal has lost the property that makes a standing branch safe to append to from
   concurrent runs, and is not eligible until that is repaired.
8. **Maintainer merge.** Eligibility is not activation. The proposal stays unmerged until a
   maintainer merges it to `main`, and takes effect only on the next clean runtime load. No run
   ever activates its own proposal.

A proposal failing 1 or 7 is a defect in the pipeline, not in the idea. A proposal failing 4 or 6
is simply not ready. A proposal failing 5 is not rejected: it is re-filed on the other stream.
