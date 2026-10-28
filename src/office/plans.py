"""Plans, plan review, and the dispatch barrier.

Convergence contract (#337, office.contract), plan-review lifecycle (#418):

  Plan review reviews the initial plan. The cycle is open from the first
  submit until it closes; only an open cycle queues a reviewer
  (`can_auto_queue`). Once closed, no amendment, resume or late result
  reopens it: the planner/orchestrator owns every later revision. Only the
  user reopens review, explicitly (`office review plan --quote ...`), as a
  new bounded cycle recorded as user-requested.

  APPROVED    dispatch-safe: the cycle closes and every eligible executor may
              fan out. Its findings stay tracked until the orchestrator or
              planner fixes or dispositions them, without another review.
  RECHECK     the planner revises; tasks the blocking findings name (and their
              dependants) wait, plan-wide findings hold every task, and the
              same reviewer (when available) reviews the revision. At the
              round cap (3 substantive rounds unless the user chose another
              at start) the cycle closes unapproved: the orchestrator owns the
              outstanding findings and records a disposition for each.
  INTAKE_GAP  the named user decision is asked at once; what it affects waits.
  Reviewer unavailability or an unreadable reply is runtime status, never a
  verdict, and spends no round.

v3.1 contract â€” fork after the first plan-review verdict
(docs/v31-rolling-review-gates.md Â§2)¶»§q«^