"""Explicit #936 greenfield build contract: one executor, 2-3 internal phases.

Planning phases are checkpoints within a single fenced task, not Office tasks.
This makes the existing convergence-v1 one-lane review exactly one independent
end-of-build review; no parallel lanes or per-task review fanout are created.
"""
from __future__ import annotations


def enabled(run: dict) -> bool:
    return (run.get("gear") == "greenfield"
            and (run.get("gates") or {}).get("gear_model_version") == "run-gear-1")


def sizing_problems(run: dict, tasks: list[dict], carried: list[dict] | None = None) -> list[str]:
    """Every task of a non-greenfield plan declares its `task_size:` (Rico 2026-11-26, Q3).

    Greenfield pins its one cohesive task to XL itself, so it keeps its own rule in `plan_problems`.
    `carried` is the current plan's tasks: an amendment may keep a task that plan already left unsized
    (a run approved before this rule), but a task it adds, or one it unsizes, is refused.
    """
    if run.get("gear") == "greenfield":
        return []
    legacy = {t.get("id") for t in carried or [] if not (t.get("descriptor") or {}).get("task_size")}
    missing = [t["id"] for t in tasks if not (t.get("descriptor") or {}).get("task_size") and t["id"] not in legacy]
    if not missing:
        return []
    return [f"{', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no `task_size:`; add `task_size: XS | S | M | L | XL` "
            "and `task_size_rationale:` to every task (Office routes the executor by task size)"]


def plan_problems(run: dict, tasks: list[dict]) -> list[str]:
    """Reject decomposition that silently recreates the handoff tax."""
    if not enabled(run):
        return []
    if len(tasks) != 1:
        return ["greenfield gear requires exactly one cohesive executor task with 2-3 internal phases "
                "(no separate Office task chain); choose another gear for multiple workstreams"]
    task = tasks[0]
    phases = task.get("phases") or []
    problems: list[str] = []
    if len(phases) not in (2, 3):
        problems.append("greenfield task needs 2-3 `phases:` list items (large implementation checkpoints "
                        "inside this one executor session)")
    if any(not isinstance(p, str) or not p.strip() for p in phases):
        problems.append("greenfield phases must each have a nonempty description")
    if len({p.strip().casefold() for p in phases if isinstance(p, str)}) != len(phases):
        problems.append("greenfield phases must be distinct")
    if task.get("depends"):
        problems.append("greenfield task cannot depend on another task; all phases belong to the same build")
    declared = (task.get("descriptor") or {}).get("task_size")
    if declared and declared != "XL":
        problems.append("greenfield uses an XL-sized strong executor route; remove `task_size:` or set it to XL")
    return problems
