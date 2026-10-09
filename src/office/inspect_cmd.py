"""office inspect: the deep, on-demand view. Hashes, paths, event ids and
routing detail live here, never in default output."""
from __future__ import annotations

import json

from office import adaptive, candidates, contract, plans, state
from office import risk as risk_mod
from office.result import Result
from office.state import Usage
from office.util import loads, short


def _optional_cols(con) -> str:
    """session_id / resumed_from / harness, or NULL stand-ins on a schema that predates them."""
    have = {r[1] for r in con.execute("PRAGMA table_info(dispatches)")}
    return ", ".join(c if c in have else f"NULL AS {c}" for c in ("session_id", "resumed_from", "harness"))


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
    if what == "learner":
        return _learner(con, run)
    if what == "trust":
        return Result(lines=candidates.trust_report(con)[1])
    if what in ("convergence", "lane", "lanes", "scope"):
        from office import convergence
        lines = convergence.inspect_lines(con, run, ident)
        return Result(lines=lines, data=convergence.receipt(con, run) if contract.is_convergence(run) else {})
    raise Usage("unknown-view", f"cannot inspect {what!r}",
                next_step="office inspect run|plan|task|gate|evidence|events|route|learner|trust|convergence [id]")


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
    lines = [f"run {run['id']} ({run['phase']}) office {run['office_version']} | review contract {contract.of(run)}",
             f"goal: {run['goal']}", f"gear {run['gear']} | planner {run.get('planner_mode')} | base {run['base_sha'][:12]}",
             f"requirements r{run['requirements_version']}: {json.dumps(req['frozen'])[:300]}",
             f"plan p{run['plan_version']}" + (f" ({plan['kind']}, {plan['content_hash'][7:19]})" if plan else ""),
             f"plan review: {json.dumps({k: v for k, v in rs.items() if k != 'open_defects'})}",
             f"envelope: {json.dumps(run.get('envelope'))[:300]}",
             *([f"review policy: {rl}"] if (rl := risk_mod.line(run)) else []),
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
                                     "requirements": req["frozen"], "plan_review": rs, "risk": risk_mod.summary(run)})


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
                     + (f" | session {d['session_id']}" if d["session_id"]
                        else f" | session unavailable: {d['harness'] or 'harness'} exposes none"))
    for r in con.execute("SELECT id, commit_sha, status, applied_version, created_at FROM revisions WHERE run_id=? AND task_id=? "
                         "ORDER BY seq", (run["id"], tid)).fetchall():
        lines.append(f"revision {r['id']} {r['commit_sha'][:12]} {r['status']} applied p{r['applied_version']}")
        for g in con.execute("SELECT id, kind, status, verdict, evidence_status, round, escalated, route, summary, contract "
                             "FROM gates WHERE revision_id=? ORDER BY created_at", (r["id"],)).fetchall():
            # Stored verdicts are shown as stored; a v3.1 one is labelled, never translated.
            shown = contract.display_verdict(g["verdict"], g["contract"]) if g["verdict"] else ""
            lines.append(f"  gate {g['id']} {g['kind']} {g['status']} {shown} {g['evidence_status'] or ''} "
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
    lines = [f"{g['id']} {g['subject']} {g['task_id'] or g['scope'] or ''} {g['kind']} "
             f"rev {g['revision_id'] or 'p' + str(g['plan_version'])} {g['status']} "
             f"{contract.display_verdict(g['verdict'], g['contract']) if g['verdict'] else ''} "
             f"{('status=' + g['review_status']) if g['review_status'] else ''} {g['evidence_status'] or ''} "
             f"{g['independence'] or ''} key {g['input_key'][:16]} {g['route'] or ''}"
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
    """`route T1`: the recorded decisions for a task (plan slate, each dispatch).
    `route [role]`: a live decision now (no quota probe), with its evidence matrix."""
    if role_or_task and role_or_task.upper().startswith("T") and role_or_task[1:].isdigit():
        return _route_task(con, run, role_or_task.upper())
    role = role_or_task or "executor"
    decision = candidates.route_role(con, state.pinned_config(run), run, role, probe=False)
    lines = [f"{role}: {decision.get('status')} -> {decision.get('selected')} (live, quota not probed)"]
    if risk_mod.line(run):
        lines.append(f"review policy: {risk_mod.line(run)}")
    if decision.get("routing"):
        lines += adaptive.render_slate(decision.get("slate") or [], indent="")
        lines += _matrix(decision["routing"])
    elif decision.get("selection_disclosure"):
        lines.append(f"reason: {decision['selection_disclosure'].get('reason')}")
    lines += _rejections(decision)
    data = {k: v for k, v in decision.items() if k not in ("request", "qualifying_candidates")}
    return Result(lines=lines, data=data)


def _rejections(decision: dict) -> list[str]:
    lines = [f"rejected {r['candidate']} stage {r['stage']}: {r['reason']}" for r in (decision.get("rejected") or [])[:12]]
    lines += [f"skipped {s['candidate']}: {s['reason']}" for s in (decision.get("skipped") or [])[:8]]
    return lines


def _matrix(audit: dict) -> list[str]:
    """Every qualifying candidate's score components, best first."""
    rows = sorted(audit.get("candidates") or [], key=lambda r: r.get("rank") or 99)
    lines = ["", f"evidence ({audit.get('policy_version')}, {audit.get('learner_version')}, "
                 f"{audit.get('cost_policy')}, as of {(audit.get('evidence_as_of') or '')[:16]})",
             f"{'#':>2} {'route':<38} {'p(ok)':>6} {'local n':>7} {'bench':>5} {'$/task':>7} {'min':>6} "
             f"{'quota':>9} {'pref':>4} {'util':>6}  band"]
    if audit.get("task_descriptor"):
        lines.append("task descriptor: " + json.dumps(audit["task_descriptor"], sort_keys=True))
    for r in rows:
        if (r.get("benchmark") or {}).get("task_fit"):
            fit = r["benchmark"]["task_fit"]
            if fit.get("applied"):
                lines.append(f"  {r['route']} calibrated task fit: " +
                             ", ".join(f"{x['dimension']} ({x['benchmark']} {x['version']})" for x in fit["applied"]))
        if (r.get("pricing") or {}).get("tier") not in (None, "flat"):
            lines.append(f"  {r['route']} pricing tier: {r['pricing']['tier']}")
        cost = f"{r['cost_to_success']:.2f}" if r.get("cost_to_success") is not None else "?"
        mins = f"{r['time_to_success_seconds'] / 60:.0f}" if r.get("time_to_success_seconds") is not None else "?"
        pref = "" if r["preference"]["seed_rank"] is None else f"#{r['preference']['seed_rank'] + 1}"
        lines.append(f"{r.get('rank', '?'):>2} {r['label']:<38} {r['p_success']:>6.2f} {r['local']['n_effective']:>7.1f} "
                     f"{r['benchmark']['authority']:>5.2f} {cost:>7} {mins:>6} {r['quota']['state'][:9]:>9} {pref:>4} "
                     f"{r['utility']:>6.3f}  {'yes' if r.get('in_competitive_band') else ''}")
    c, x = audit.get("clincher") or {}, audit.get("exploration") or {}
    lines.append(f"close call: {'drew ' + str(c.get('draw')) + ' -> ' + str(c.get('picked')) if c.get('used') else 'no'}"
                 f" | exploration: {('picked ' + x['picked']) if x.get('picked') else x.get('blocked') or 'not drawn'}"
                 f" | seed {str(c.get('seed'))[:19]}")
    if (audit.get("spread") or {}).get("applied"):
        lines.append(f"spread: {audit['spread']['wave_load']}")
    lines.append("bench = benchmark prior authority (1 = no local evidence yet); $/task = expected cost to success")
    return lines


def _route_task(con, run, tid) -> Result:
    from office import route_learning
    route_learning.ensure_schema(con)
    rows = [dict(r) for r in con.execute("SELECT * FROM route_audit WHERE run_id=? AND task_id=? ORDER BY created_at, rowid",
                                         (run["id"], tid)).fetchall()]
    lines, audits = [], []
    for row in rows:
        audit = loads(row["disclosure_json"], {})
        audits.append({**{k: v for k, v in row.items() if k != "disclosure_json"}, "disclosure": audit})
    plan_rows = [a for a in audits if a["phase"] == "plan"]
    if plan_rows:
        a = plan_rows[-1]
        planner = a["disclosure"].get("planner") or {}
        lines.append(f"{tid} plan p{a['plan_version']} route ({planner.get('chooser', 'router')} chose; "
                     f"decision {str(a['decision_hash'])[:19]})")
        lines += adaptive.render_slate(adaptive.slate_for(a["disclosure"], planner), indent="")
        if planner.get("why"):
            lines.append(f"planner reason: {planner['why']}")
        if planner.get("planner_error"):
            lines.append(f"planner route ignored: {planner['planner_error']}")
        lines += _matrix(a["disclosure"])
    for a in (x for x in audits if x["phase"] != "plan"):
        d = a["disclosure"].get("dispatch") or {}
        taken = "; ".join(f"{t['route']}: {t['reason']}" for t in d.get("fallbacks_taken") or [])
        lines.append(f"{a['phase']} {a['created_at'][:16]} -> {a['dispatched_route']} ({d.get('source')})"
                     + (f" after fallback: {taken}" if taken else ""))
    legacy = []
    for d in con.execute("SELECT id, triple, route_json FROM dispatches WHERE run_id=? AND task_id=? ORDER BY started_at",
                         (run["id"], tid)).fetchall():
        route = loads(d["route_json"], {}) or {}
        if not route.get("audit_id") and not route.get("route_source"):
            reason = (route.get("selection_disclosure") or {}).get("reason") or ""
            legacy.append(f"dispatch {d['id']} {d['triple']} (single-route record): {reason[:160]}")
    lines += legacy
    if not lines:
        lines = [f"no routing recorded for {tid}; office inspect route shows a live decision"]
    return Result(lines=lines, data={"task": tid, "audits": audits, "legacy": legacy})


def _learner(con, run) -> Result:
    """The route learner's state: outcomes by route and attribution, learned
    eligibility, and the changes the evidence would make at the next run close."""
    from office import route_learning
    route_learning.ensure_schema(con)
    outcomes = route_learning.derive_outcomes(con)
    eps = route_learning.episodes(outcomes)
    by_route: dict[str, dict] = {}
    for e in eps:
        b = by_route.setdefault(e["route"], {"episodes": 0, "landed": 0, "failed": {}})
        b["episodes"] += 1
        if e["success"]:
            b["landed"] += 1
        else:
            b["failed"][e["attribution"]] = b["failed"].get(e["attribution"], 0) + 1
    lines = [f"learner {route_learning.LEARNER_VERSION}: {len(outcomes)} dispatch outcomes, {len(eps)} task-route episodes"]
    for route, b in sorted(by_route.items(), key=lambda kv: -kv[1]["episodes"])[:15]:
        failed = ", ".join(f"{k} {v}" for k, v in sorted(b["failed"].items()))
        lines.append(f"  {route:<44} {b['landed']}/{b['episodes']} landed" + (f" | failed: {failed}" if failed else ""))
    priors = candidates.learner_priors(con, state.pinned_config(run))
    pending, current = [], {}
    for role in route_learning.ADAPTIVE_ROLES:
        current[role] = route_learning.current_eligibility(con, role)
        for route, ev in current[role].items():
            lines.append(f"{role} {route}: {ev['state']} ({ev['event_id']}, {ev['at'][:10]})")
        role_eps = [e for e in eps if e["role"] == role]
        for tr in route_learning.eligibility_transitions(role_eps, priors.get(role) or {},
                                                         route_learning.current_eligibility_all(con, role)):
            pending.append({"role": role, **tr})
            lines.append(f"pending at next close: {role} {tr['route']} {tr['previous_state']} -> {tr['state']} "
                         f"(n={tr['evidence']['samples']}, replay: {tr['replay']['reason']})")
    return Result(lines=lines, data={"routes": by_route, "eligibility": current, "pending": pending})
