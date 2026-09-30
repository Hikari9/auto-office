"""office inspect: the deep, on-demand view. Hashes, paths, event ids and
routing detail live here, never in default output."""
from __future__ import annotations

import json

from office import candidates, plans, state
from office.result import Result
from office.state import Usage
from office.util import loads, short


def _optional_cols(con) -> str:
    """session_id / resumed_from, or NULL stand-ins on a schema that predates them."""
    have = {r[1] for r in con.execute("PRAGMA table_info(dispatches)")}
    return ", ".join(c if c in have else f"NULL AS {c}" for c in ("session_id", "resumed_from"))


def inspect(con, run: dict, what: str | None, ident: str | None) -> Result:
    what = (what or "run").lower()
    if what == "run":
        return _run(con, run)
    if what == "task":
        if not ident:
            raise Usage("missing-id", "name the task", next_step="office inspect task T2")
        return _task(con, run, ident.upper())
    if what == "gate":
        return _gate(con, run, ident)
    if what == "evidence":
        return _evidence(con, run, ident)
    if what == "events":
        return _events(con, run, ident)
    if what == "route":
        return _route(con, run, ident)
    if what == "plan":
        return _plan(con, run, ident)
    raise Usage("unknown-view", f"cannot inspect {what!r}",
                next_step="office inspect run|plan|task|gate|evidence|events|route [id]")


def _plan(con, run, ident) -> Result:
    """The plan diagram: current version, or `pN` for an earlier one."""
    from office import plan_view
    version = int(str(ident).lstrip("pP")) if ident else run["plan_version"]
    pv = plan_view.load(con, run["id"], version) if version else None
    if not pv:
        raise Usage("no-diagram", f"no diagram recorded for plan p{version}", next_step="office submit")
    return Result(lines=plan_view.render(run, version, pv), data={"diagram": pv})


def _run(con, run) -> Result:
    run = state.get_run(con, run["id"])
    req = state.current_requirements(con, run["id"])
    plan = state.current_plan(con, run["id"])
    rs = plans.review_state(con, run)
    lines = [f"run {run['id']} ({run['phase']}) office {run['office_version']}",
             f"goal: {run['goal']}", f"gear {run['gear']} | planner {run.get('planner_mode')} | base {run['base_sha'][:12]}",
             f"requirements r{run['requirements_version']}: {json.dumps(req['frozen'])[:300]}",
             f"plan p{run['plan_version']}" + (f" ({plan['kind']}, {plan['content_hash'][7:19]})" if plan else ""),
             f"plan review: {json.dumps({k: v for k, v in rs.items() if k != 'open_defects'})}",
             f"envelope: {json.dumps(run.get('envelope'))[:300]}",
             f"state dir: {run['state_dir']}", f"policy {run['policy_hash'][:19]} config {run['config_hash'][:19]}"]
    for a in con.execute("SELECT kind, target, requirements_version, quote, created_at FROM authorizations WHERE run_id=? "
                         "ORDER BY created_at", (run["id"],)).fetchall():
        lines.append(f"authority {a['kind']} {a['target'] or ''} r{a['requirements_version']}: \"{a['quote'][:80]}\"")
    for t in state.tasks(con, run["id"], include_planner=True):
        lines.append(f"{t['id']} {t['status']} rev {t['current_revision_id'] or '-'} accepted {t['accepted_revision_id'] or '-'}"
                     + (f" ({t['pause_reason']})" if t.get("pause_reason") else ""))
    integ = (run.get("landing") or {}).get("integration")
    if integ:
        lines.append(f"integration {integ.get('status')} {integ.get('branch', '')} {integ.get('detail', '')}")
    return Result(lines=lines, data={"run": {k: v for k, v in run.items() if not k.endswith("_json")},
                                     "requirements": req["frozen"], "plan_review": rs})


def _task(con, run, tid) -> Result:
    t = state.get_task(con, run["id"], tid)
    if t is None:
        raise Usage("unknown-task", f"no task {tid}", next_step="office inspect run")
    lines = [f"{tid} {t['title']} | {t['status']}" + (f" ({t['pause_reason']})" if t.get("pause_reason") else ""),
             f"scope {', '.join(t['scope'])} | depends {', '.join(t['depends']) or 'none'}",
             f"contract p{t['contract_version']} acceptance p{t['acceptance_version']} escalations {t['escalations_used']}"]
    if t.get("review_override"):
        lines.append(f"review pinned by user: {t['review_override']['as']}"
                     + (" (external)" if t["review_override"].get("external") else ""))
    for d in con.execute("SELECT id, role, triple, status, terminal_classification, exit_code, applied_plan_version, worktree, "
                         "launcher, override_json, " + _optional_cols(con) + " FROM dispatches WHERE run_id=? AND task_id=? "
                         "ORDER BY started_at",
                         (run["id"], tid)).fetchall():
        ov = json.loads(d["override_json"] or "{}")
        lines.append(f"dispatch {d['id']} {d['role']} {d['triple']} {d['status']} {d['terminal_classification'] or ''} "
                     f"applied p{d['applied_plan_version'] or '-'} via {d['launcher'] or '-'}"
                     + (" | user override" + (f" --cli {ov['cli']}" if ov.get("cli") else "") if ov.get("by") else "")
                     + (f" | resumed from {d['resumed_from'] or ov.get('resumed_from')}"
                        if (d["resumed_from"] or ov.get("resumed_from")) else "")
                     + (f" | session {d['session_id']}" if d["session_id"] else ""))
    for r in con.execute("SELECT id, commit_sha, status, applied_version, created_at FROM revisions WHERE run_id=? AND task_id=? "
                         "ORDER BY seq", (run["id"], tid)).fetchall():
        lines.append(f"revision {r['id']} {r['commit_sha'][:12]} {r['status']} applied p{r['applied_version']}")
        for g in con.execute("SELECT id, kind, status, verdict, evidence_status, round, escalated, route, summary FROM gates "
                             "WHERE revision_id=? ORDER BY created_at", (r["id"],)).fetchall():
            lines.append(f"  gate {g['id']} {g['kind']} {g['status']} {g['verdict'] or ''} {g['evidence_status'] or ''} "
                         f"round {g['round']}{' escalated' if g['escalated'] else ''} {g['route'] or ''} {(g['summary'] or '')[:100]}")
    for f in con.execute("SELECT code, gate_kind, severity, state, location, summary FROM findings WHERE run_id=? AND task_id=? "
                         "ORDER BY created_at", (run["id"], tid)).fetchall():
        lines.append(f"finding {f['code']} {f['gate_kind']} {f['severity']} {f['state']} {f['location'] or ''} {f['summary'][:100]}")
    for dl in con.execute("SELECT amendment_id, status, target_version, delivered_count FROM deliveries WHERE run_id=? AND task_id=?",
                          (run["id"], tid)).fetchall():
        lines.append(f"amendment {dl['amendment_id']} -> p{dl['target_version']} {dl['status']} (delivered {dl['delivered_count']}x)")
    return Result(lines=lines, data={"task": t})


def _gate(con, run, gid) -> Result:
    q = "SELECT * FROM gates WHERE run_id=?" + (" AND id=?" if gid else " ORDER BY created_at DESC LIMIT 20")
    rows = con.execute(q, (run["id"], gid) if gid else (run["id"],)).fetchall()
    lines = [f"{g['id']} {g['subject']} {g['task_id'] or ''} {g['kind']} rev {g['revision_id'] or 'p' + str(g['plan_version'])} "
             f"{g['status']} {g['verdict'] or ''} {g['evidence_status'] or ''} key {g['input_key'][:16]} {g['route'] or ''}"
             for g in rows]
    return Result(lines=lines or ["no gates"], data={"gates": [dict(g) for g in rows]})


def _evidence(con, run, ident) -> Result:
    q = "SELECT * FROM evidence WHERE run_id=?" + (" AND (task_id=? OR gate_id=? OR revision_id=?)" if ident else "")
    args = (run["id"], ident, ident, ident) if ident else (run["id"],)
    rows = con.execute(q + " ORDER BY created_at DESC LIMIT 40", args).fetchall()
    lines = [f"{e['id']} {e['kind']} {e['task_id'] or ''} {e['revision_id'] or ''} {(e['sha256'] or '')[:19]} {e['path'] or ''}"
             for e in rows]
    return Result(lines=lines or ["no evidence"], data={"evidence": [dict(e) for e in rows]})


def _events(con, run, ident) -> Result:
    rows = con.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 40", (run["id"],)).fetchall()
    lines = [f"#{e['seq']} {e['created_at'][11:19]} {e['audience']} {e['kind']} {e['summary'][:120]}" for e in reversed(rows)]
    return Result(lines=lines or ["no events"], data={"events": [dict(e) for e in rows]})


def _route(con, run, role_or_task) -> Result:
    role = "executor"
    if role_or_task and not role_or_task.upper().startswith("T"):
        role = role_or_task
    decision = candidates.route_role(con, state.pinned_config(run), run, role, probe=False)
    lines = [f"{role}: {decision.get('status')} -> {decision.get('selected')}"]
    if decision.get("selection_disclosure"):
        lines.append(f"reason: {decision['selection_disclosure'].get('reason')}")
    for r in (decision.get("rejected") or [])[:12]:
        lines.append(f"rejected {r['candidate']} stage {r['stage']}: {r['reason']}")
    for s in (decision.get("skipped") or [])[:8]:
        lines.append(f"skipped {s['candidate']}: {s['reason']}")
    return Result(lines=lines, data={k: v for k, v in decision.items() if k != "request"})
