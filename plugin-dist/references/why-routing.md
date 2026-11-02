# Why: role-routing rationale

Rationale for pinned rules in the "Role routing" section of `SKILL.md`.

## Why local evidence outranks public benchmark data

Public benchmark data is a cold-start prior only: it describes average behavior across tasks
unlike this one. Once enough comparable local evidence exists for the exact routable triple
(`harness@major × model_id × effort`), that evidence reflects this codebase, this adapter, and
this task shape more directly than any published benchmark, so it wins the tie-break. This is why
an absolute floor is never lowered for cost or quota: floors exist to catch cases local evidence
hasn't covered yet, and a cheaper/faster route is never allowed to trade away that floor.

## Why the route notice must cite decisive evidence

A route notice that says "this model is high quality" is unfalsifiable and cannot be checked
against what actually decided the route. Citing task-shape fit, capability/trust floor,
preferred seed, quota, cost, or comparable local results makes the disclosure a receipt: a third
party can re-check whether the cited evidence actually supports the routed identity.

## Why executor routing recommends a slate instead of a winner (#300)

A single deterministic winner let one cheap route absorb every task: the balanced money band removed every
route more than 20% above the cheapest token price before effectiveness or quota was read, and effort was not
priced at all. Expected cost to success (attempt cost × attempts ÷ success rate) is what a task actually costs,
so a route with a higher token price can be the cheaper one. Returning three strong routes lets the planner
weigh what the score cannot see (a precise plan that a cheaper builder can follow, a harness that cannot reach a
resource), and lets dispatch fall back without inventing intent. Close calls are broken by a seeded draw so
equally good routes share the work, and a small, capped exploration rate keeps today's favourite from becoming
the only route with evidence.
