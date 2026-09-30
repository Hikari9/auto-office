"""Plan-defect redirects: the user's answer to a defect rooted in a requirement.

A PLAN_DEFECT says a requirement or assumption the plan rests on is unachievable
or wrong; CHANGES_REQUIRED is a plan amendment. When the planner traces a defect
to a requirement or assumption, it asks the user how to redirect it and records
the answer here, with the user's words. A redirect can record requirements
r(n+1) (the same quote authorizes it when r(n) was authorized), resets the
plan-review round budget, and picks the next reviewer: the same session resumed,
or a fresh reviewer on another route. The defect itself still clears only when
an independent reviewer says CLEARED. The user may instead waive the defect
(`office approve waive P<n>`), which closes it without another review.
"""
from __future__ import annotations

import json
import uuid

from office import plans, review_parse, state
from office.state import Refused, Usage
from office.util import now_iso

REVIEWERS = ("same", "fresh")
AMEND_FORM = ('office amend plan --contract --redirect P<n> --root-cause "<requirement or assumption>" '
              '--quote "<user\'s words>" [--requirement "<new requirement>"] [--reviewer same|fresh] -- "<fix>"')
SUBMIT_FORM = ('office submit --redirect P<n> --root-cause "<requirement or assumption>" --quote "<user\'s words>" '
               '[--requirement "<new requirement>"] [--reviewer same|fresh]')


def open_defect(con, run: dict, code: str) -> dict | None:
    """The open plan defect named `code` (plan-review classes only, not BRIEF_DEFECT)."""
    row = con.execute("SELECT * FROM findings WHERE run_id=? AND gate_kind='plan_review' AND code=? AND state='open' "
                      "AND category IN (?,?,?,?)", (run["id"], code, *review_parse.DEFECT_CLASSES)).fetchone()
    return dict(row) if row else None


def validate(con, run: dict, spec: dict, *, form: str = AMEND_FORM) -> dict:
    """Normalize a redirect request before anything is written."""
    code = (spec.get("defect") or "").strip().upper()
    quote = (spec.get("quote") or "").strip()
    root_cause = (spec.get("root_cause") or "").strip()
    reviewer = (spec.get("reviewer") or "fresh").strip().lower()
    if not quote:
        raise Usage("user-quote-required", "a redirect records the user's own words", next_step=form)
    if not root_cause:
        raise Usage("root-cause-required", "name the requirement or assumption that causes the defect", next_step=form)
    if reviewer not in REVIEWERS:
        raise Usage("bad-reviewer", f"--reviewer is same or fresh, not {reviewer!r}", next_step=form)
    defect = open_defect(con, run, code)
    if defect is None:
        codes = ", ".join(d["code"] for d in plans.open_defects(con, run["id"]) if d["category"] != "brief")
        raise Refused("no-open-defect", f"{code or '(none)'} is not an open plan defect",
                      next_step=f"open plan defects: {codes}" if codes else "office status")
    gate = con.execute("SELECT id, route FROM gates WHERE id=?", (defect["gate_id"],)).fetchone()
    reviewer_row = con.execute("SELECT id FROM dispatches WHERE gate_id=? AND role='plan_reviewer' "
                               "ORDER BY started_at DESC LIMIT 1", (defect["gate_id"],)).fetchone()
    return {"defect": code, "quote": quote, "root_cause": root_cause,
            "requirement": (spec.get("requirement") or "").strip() or None, "reviewer": reviewer,
            "summary": defect["summary"], "category": defect["category"],
            "origin_dispatch": reviewer_row["id"] if reviewer_row else defect.get("reviewer_dispatch_id"),
            "origin_route": gate["route"] if gate else None}


def record(con, run: dict, spec: dict) -> list[str]:
    """Record a validated redirect. Caller holds the tx. Returns result lines."""
    from office import amend
    lines = []
    run = state.get_run(con, run["id"])
    requirements_version = None
    if spec.get("requirement"):
        authorized = state.active_authorization(con, run, "plan") is not None
        requirements_version = amend.record_requirements(con, run, spec["requirement"], spec["quote"])
        run = state.get_run(con, run["id"])
        if authorized:
            # The user's redirect is their decision on r(n+1); it needs no second prompt.
            con.execute("INSERT INTO authorizations(id, run_id, kind, target, requirements_version, envelope_json, "
                        "authorized_by, quote, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("Z" + uuid.uuid4().hex[:8], run["id"], "plan", "requirements", requirements_version,
                         json.dumps(run.get("envelope") or []), "user", spec["quote"], now_iso()))
        lines.append(f"requirements r{requirements_version} recorded"
                     + (" and authorized by the redirect" if authorized else " | authorization required"))
    # A queued round would review the plan the redirect replaces.
    con.execute("UPDATE gates SET status='cancelled', stale_reason='superseded by a defect redirect' WHERE run_id=? "
                "AND subject=? AND status='queued'", (run["id"], plans.PLAN_SUBJECT))
    gates = plans.plan_gates(con, run["id"])
    pr = dict(run.get("plan_review") or {})
    pr.pop("ended_reason", None)
    entry = {"defect": spec["defect"], "root_cause": spec["root_cause"], "quote": spec["quote"],
             "requirement": spec.get("requirement"), "requirements_version": requirements_version,
             "reviewer": spec["reviewer"], "from_plan_version": run["plan_version"], "at": now_iso()}
    pr.update({"ended": False, "round_base": len([g for g in gates if not g["escalated"]]),
               "next_reviewer": {"mode": spec["reviewer"], "defect": spec["defect"],
                                 "dispatch": spec.get("origin_dispatch"), "route": spec.get("origin_route")},
               "redirects": [*(pr.get("redirects") or []), entry]})
    state.update_run(con, run["id"], plan_review=pr, escalations_used=0)
    state.emit(con, run, "plan.redirected",
               f"plan defect {spec['defect']} redirected by the user (root cause: {spec['root_cause'][:80]}); "
               f"review budget reset, next reviewer {spec['reviewer']}",
               payload=entry)
    lines.append(f"defect {spec['defect']} redirected | review budget reset | next reviewer: {spec['reviewer']}")
    return lines


def planner_note(spec: dict) -> str:
    """The redirect as the dedicated planner reads it in its contract request."""
    out = [f"USER REDIRECT for plan defect {spec['defect']} ({spec.get('category')}): {spec.get('summary', '')[:300]}",
           f"  root cause: {spec['root_cause']}", f"  the user said: \"{spec['quote']}\""]
    if spec.get("requirement"):
        out.append(f"  requirement change: {spec['requirement']}")
    out.append("Revise the plan to follow this redirect so the defect no longer holds.")
    return "\n".join(out)


def brief_lines(run: dict, defects: list[dict]) -> list[str]:
    """Redirect context for the plan reviewer, one block per open defect it concerns."""
    codes = {d["code"] for d in defects}
    out = []
    for r in (run.get("plan_review") or {}).get("redirects") or []:
        if r["defect"] not in codes:
            continue
        out.append(f"USER REDIRECT {r['defect']}: the planner traced it to: {r['root_cause']}")
        out.append(f"  the user decided: \"{r['quote']}\"")
        if r.get("requirement"):
            out.append(f"  requirements r{r['requirements_version']}: {r['requirement']}")
    if out:
        out.append("Judge the defect against the redirected requirements. CLEARED it only if it no longer holds.")
    return out


def take_next_reviewer(con, run: dict) -> dict | None:
    """Consume the reviewer choice a redirect left for the next round. Caller holds the tx."""
    run = state.get_run(con, run["id"])
    pr = dict(run.get("plan_review") or {})
    choice = pr.pop("next_reviewer", None)
    if choice is not None:
        state.update_run(con, run["id"], plan_review=pr)
    return choice


def waive(con, run: dict, code: str, quote: str, root_cause: str | None = None) -> str:
    """The user judged the defect wrong: close it without another review. Caller holds the tx."""
    code = code.upper()
    if open_defect(con, run, code) is None:
        raise Refused("no-open-defect", f"{code} is not an open plan defect", next_step="office status")
    con.execute("UPDATE findings SET state='waived', updated_at=? WHERE run_id=? AND gate_kind='plan_review' "
                "AND code=? AND state='open'", (now_iso(), run["id"], code))
    pr = dict(run.get("plan_review") or {})
    pr["waived_defects"] = [*(pr.get("waived_defects") or []),
                            {"defect": code, "quote": quote.strip(), "root_cause": root_cause, "at": now_iso()}]
    state.update_run(con, run["id"], plan_review=pr)
    plans.unpause_cleared(con, run)
    state.emit(con, run, "authority.waiver", f"user waived plan defect {code}"
               + (f" (root cause: {root_cause[:80]})" if root_cause else ""))
    return f"plan defect {code} waived by the user (recorded)"
