"""Plan review reviews the initial plan only (#418).

The initial cycle may iterate through RECHECK rounds up to its cap (3, or the
user's `office start --plan-review-rounds N`). Once it closes (APPROVED, the cap
spent, or waived), no amendment, resume or late result reopens it; only the
user's explicit `office review plan --quote ...` opens another, bounded cycle.

Jobs run manually here, so every plan-review round stays queued until the test
records its result through `plans.ingest_plan_review`: what queues a reviewer
is observed directly. No agent runs, except the dedicated-planner test, which
uses the scripted in-process fakes.
"""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE, PLAN_TWO

MANUAL = {"OFFICE_JOBS": "manual"}
GEAR = "express"
APPROVED = "VERDICT: APPROVED\nNEXT proceed"


def recheck(code="P1", where="T1", what="the contract is unclear"):
    return f"VERDICT: RECHECK\nFINDING {code} | medium | blocking | {where} | {what} | fix it\nNEXT revise"


def _o(env, *args, check=None, extra_env=None):
    return env.office(*args, check=check, env={**MANUAL, **(extra_env or {})})


def _q(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _run(env):
    row = _q(env, "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")[0]
    for k in ("gates", "plan_review"):
        row[k] = json.loads(row[f"{k}_json"] or "{}")
  ¶»§q«^