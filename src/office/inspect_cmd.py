"""office inspect: the deep, on-demand view. Hashes, paths, event ids and
routing detail live here, never in default output."""
from __future__ import annotations

import json

from office import adaptive, candidates, contract, plans, route_learning, route_policy, routing, state
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
    if what in ("amendments", "amendment"):
        return _amendments(con, run, ident)
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
                next_step="office inspect run|plan|task|gate|evidence|amendments|events|route|learner|trust|convergence [id]")


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


def _amendments(con, run, ident) -> Result:
    """Each amendment with its rationale and, when it changed a task contract, the old and effective contract."""
    rows = con.execute("SELECT * FROM amendments WHERE run_id=? ORDER BY seq", (run["id"],)).fetchall()
    if ident:
        rows = [r for r in rows if r["id"].endswith(":" + ident.upper()) or r["id"] == ident]
    lines, data = [], []
    for a in rows:
        rec = loads(a["structured_json"]) if a["structured_json"] else {}
        label = a["id"].split(":")[-1]
        lines.append(f"{label} {a['class']} p{a['from_plan_version']}->p{a['to_plan_version'] or '-'} {a['status']} "
                     f"by {a['requested_by']}: {a['delta'][:160]}")
        for tid, ch in (rec.get("changed") or {}).items():
            for key in ("scope", "depends", "interfaces", "accept", "checks"):
                old, new = (ch["before"] or {}).get(key), (ch["after"] or {}).get(key)
                if old != new:
                    lines.append(f"  {tid} {key}: {json.dumps(old)} -> {json.dumps(new)}"
                                 if ch["before"] and ch["after"] else f"  {tid} {'added' if ch['after'] else 'removed'}")
        data.append({**dict(a), "structured": rec})
    return Result(lines=lines or ["no amendments"], data={"amendments": data})


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
    categories = _categories(decision)
    config = state.pinned_config(run)
    policies = [*candidates.current_user_policies(run.get("repo_root")), route_policy.user_policy(config)]
    lines += _discovery_lines(decision, categories, config, policies)
    trials = _trial_rows(con, run, role=role)
    lines += _trial_lines(trials) + _attempt_lines(route_learning.attempt_history(con, run_id=run["id"], role=role, include_unbound=True))
    data = {k: v for k, v in decision.items() if k not in ("request", "qualifying_candidates")}
    if categories:
        data["categories"] = categories
    if trials:
        data["trials"] = trials
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
    lines, audits = _effective_route_lines(con, run, tid), []
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
        disc = a["disclosure"].get("discovery")
        if disc:
            lines.append("  " + _discovery_head(disc))
    trials = _trial_rows(con, run, task_id=tid)
    attempts = route_learning.attempt_history(con, run_id=run["id"], task_id=tid)
    lines += _trial_lines(trials) + _attempt_lines(attempts)
    legacy = []
    for d in con.execute("SELECT id, triple, route_json FROM dispatches WHERE run_id=? AND task_id=? ORDER BY started_at",
                         (run["id"], tid)).fetchall():
        route = loads(d["route_json"], {}) or {}
        if not route.get("audit_id") and not route.get("route_source"):
            reason = (route.get("selection_disclosure") or {}).get("reason") or ""
            legacy.append(f"dispatch {d['id']} {d['triple']} (single-route record): {reason[:160]}")
    lines += legacy
    changes = state.route_changes(con, run["id"], tid)
    if not lines:
        lines = [f"no routing recorded for {tid}; office inspect route shows a live decision"]
    data = {"task": tid, "audits": audits, "legacy": legacy,
            "effective_route": state.recorded_route(state.get_task(con, run["id"], tid)) or None,
            "route_changes": changes}
    if trials or attempts:
        data.update(trials=trials, attempts=attempts)
    return Result(lines=lines, data=data)


def _effective_route_lines(con, run, tid) -> list[str]:
    """The route Office will follow for `tid` and every recorded change to it (#426)."""
    task = state.get_task(con, run["id"], tid)
    rec = state.recorded_route(task) if task else {}
    lines = []
    if rec:
        how = "declared" if rec.get("declared") else (rec.get("route_source") or "recorded")
        lines.append(f"{tid} effective route {routing.candidate_id(rec['candidate'])} ({how})")
    for c in state.route_changes(con, run["id"], tid):
        lines.append(f"route change {c['recorded_at'][:16]} {c.get('before') or 'unrecorded'} -> {c['after']} "
                     f"[{c.get('kind')}] by {c.get('actor')}: {c.get('reason')}")
    return lines


# The categories an operator reads one by one. A row that is out for a reason that has nothing to do
# with discovery or policy (not installed, no launch profile, below a floor) is only counted.
_LISTED_CATEGORIES = ("denied", "overkill", "unsupported", "untried", "probe-candidate", "probe-pending",
                      "probe-passed", "probe-failed", "trial-eligible")


def _probe_text(probe: dict | None) -> str:
    if not probe:
        return "no fresh exact probe on record"
    result = probe.get("result") or "unknown"
    reason = f" ({probe['reason_class']})" if probe.get("reason_class") else ""
    fresh = "fresh" if probe.get("fresh", True) else "stale"
    return f"{result}{reason}, {fresh}" + (f", probed {probe['probed_at'][:16]}" if probe.get("probed_at") else "")


def _cap_text(name: str, cap: dict) -> str:
    used, limit = cap.get("used", 0), cap.get("max", 0)
    if name == "rolling":
        return (f"rolling {used}/{limit} of the last {cap.get('window', '?')} decisions"
                + (" (warming up: too few recorded decisions to allow one)" if cap.get("warmup") else ""))
    return f"{name} {used}/{limit} used, {max(limit - used, 0)} left"


def _discovery_head(disc: dict) -> str:
    return (f"discovery intent {disc.get('intent')}" + (f" | blocked: {disc['blocked']}" if disc.get("blocked") else "")
            + f" | candidate {disc.get('candidate') or '-'} | fallback {disc.get('fallback') or 'none'}")


def _display_category(entry: dict, probe: dict | None) -> str:
    """The category an operator reads: the router's category, refined by the exact probe record.
    A fresh failed probe naming an unsupported model/effort is `unsupported` (that exact effort only);
    an in-flight probe is `probe-pending` and a fresh pass that is not being tried now is `probe-passed`."""
    category = entry.get("category") or "rejected"
    result = (probe or {}).get("result")
    if category == "probe-failed" and (probe or {}).get("reason_class") == route_learning.UNSUPPORTED_EFFORT:
        return "unsupported"
    if category == "untried" and result in ("pending", "pass"):
        return "probe-pending" if result == "pending" else "probe-passed"
    return category


def _categories(decision: dict) -> list[dict]:
    """Every categorized candidate of a decision: rejected at routing, or left out of the request."""
    by_id = {routing.candidate_id(c): c for c in (decision.get("request") or {}).get("candidates") or []}
    disc = decision.get("discovery") or {}
    out, seen = [], set()
    for entry in [*(decision.get("rejected") or []), *(decision.get("skipped") or [])]:
        if not entry.get("category") or entry["candidate"] in seen:
            continue
        seen.add(entry["candidate"])
        probe = (by_id.get(entry["candidate"]) or {}).get("probe")
        out.append({"candidate": entry["candidate"], "category": _display_category(entry, probe),
                    "reason": entry.get("reason"), "probe": probe})
    if disc.get("intent") == "trial" and disc.get("candidate"):
        out.append({"candidate": disc["candidate"], "category": "trial-eligible", "probe": disc.get("probe"),
                    "reason": "a fresh exact probe passed and every gate still holds; falls back to "
                              f"{disc.get('fallback')}"})
    return out


def _tier_text(policies: list[dict], key: str, field: str) -> str:
    """The config tiers that set `field` of the user policy (user, repo, run), or none."""
    tiers = sorted({(p.get("sources") or {}).get(key) or "run" for p in policies if p.get(field)})
    return ", ".join(tiers) or "none"


def _discovery_lines(decision: dict, categories: list[dict], config: dict, policies: list[dict]) -> list[str]:
    disc = decision.get("discovery")
    if not disc and not categories:
        return []
    lines = [""]
    if disc:
        slate = decision.get("slate") or []
        fallback = disc.get("fallback") or (slate[1]["route"] if len(slate) > 1 else None)
        lines += [_discovery_head({**disc, "fallback": fallback}),
                  f"  primary {decision.get('selected')} | probe: {_probe_text(disc.get('probe'))}",
                  "  caps: " + " | ".join(_cap_text(name, disc["caps"][name])
                                          for name in ("probes", "trials", "rolling") if name in (disc.get("caps") or {}))]
    lines.append(f"preference source tiers: denied_models {_tier_text(policies, 'denied_models', 'denied')} | "
                 f"overkill_rules {_tier_text(policies, 'overkill_rules', 'overkill')} | "
                 f"budget ceiling {route_policy.budget_ceiling(config)['source'] or 'none'}")
    listed = [c for c in categories if c["category"] in _LISTED_CATEGORIES]
    if listed:
        lines.append("candidates by category:")
        lines += [f"  {c['category']:<{max(len(x['category']) for x in listed)}}  {c['candidate']}  "
                  f"{(c.get('reason') or '')[:140]}" for c in sorted(listed, key=lambda c: (c["category"], c["candidate"]))]
    counts: dict[str, int] = {}
    for c in categories:
        if c["category"] not in _LISTED_CATEGORIES:
            counts[c["category"]] = counts.get(c["category"], 0) + 1
    if counts:
        lines.append("also out of the pool: " + ", ".join(f"{n} {name}" for name, n in sorted(counts.items())))
    return lines


def _trial_rows(con, run: dict, *, task_id: str | None = None, role: str | None = None) -> list[dict]:
    """This run's discovery trials with where each stands: the recorded terminal outcome, else what
    the recorded gate result says now (not yet recorded), else why it never reached one."""
    outcomes = {o["dispatch_id"]: o for o in route_learning.derive_outcomes(con)}
    rows = []
    for trial in route_learning.trial_attempts(con, run["id"]).values():
        if (task_id and trial["task_id"] != task_id) or (role and trial["role"] != role):
            continue
        state_, outcome = trial["state"], outcomes.get(trial["dispatch_id"])
        if trial["terminal"]:
            result = f"{state_} (recorded)"
        elif state_ in route_learning.TRIAL_LAUNCH_STATES:
            result = "none: ended before any work" + (f" ({trial['reason_class']})" if trial["reason_class"] else "")
        elif state_ == "abandoned":
            result = "none: abandoned"
        elif outcome and outcome["work"]:
            result = ("accepted" if outcome["success"] else "not accepted") + ", not yet recorded"
        else:
            result = "in flight"
        recovered = state_ in route_learning.TRIAL_LAUNCH_STATES
        rows.append({"attempt_id": trial["attempt_id"], "task_id": trial["task_id"], "dispatch_id": trial["dispatch_id"],
                     "route": trial["route"], "fallback": trial["fallback_route"], "status": state_,
                     "recovered_launch": recovered, "outcome": result, "policy_digest": trial["policy_digest"],
                     "probe_key": trial["probe_key"], "probe": trial["probe"]})
    return rows


def _trial_lines(trials: list[dict]) -> list[str]:
    if not trials:
        return []
    lines = ["", "trials:"]
    for t in trials:
        lines.append(f"  {t['attempt_id']} {t['task_id']} dispatch {t['dispatch_id'] or 'none'} {t['route']} | "
                     f"fallback {t['fallback'] or 'none'} | status {t['status']} | outcome {t['outcome']}"
                     + (" | recovered launch: the fallback ran" if t["recovered_launch"] else ""))
    return lines


def _attempt_lines(attempts: list[dict]) -> list[str]:
    """Per-attempt history from `route_discovery_events`, one block per attempt, events in order."""
    if not attempts:
        return []
    lines = ["", "attempts (route_discovery_events):"]
    for a in attempts:
        lines.append(f"  attempt {a['attempt_id']} run {a['run_id'] or 'none'} plan "
                     f"{'p' + str(a['plan_version']) if a['plan_version'] else 'none'} dispatch "
                     f"{', '.join(a['dispatches']) or 'none'} origin {a['origin']} digest {(a['policy_digest'] or '')[:19]}")
        lines.append(f"    fingerprint {a['probe_key']}")
        lines.append(f"    route {a['route']} | reason: {a['reason']}")
        for e in a["events"]:
            lines.append(f"    {e['created_at'][11:19]} {e['kind']:<18} freshness {e['probe_freshness']:<12} "
                         f"{e['outcome'] or '-'}" + (f" ({e['reason_class']})" if e["reason_class"] else "")
                         + (f" from {e['source_attempt_id']}" if e["source_attempt_id"] else ""))
    return lines


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
    trial = _learner_trials(con, outcomes)
    lines += trial["lines"]
    return Result(lines=lines, data={"routes": by_route, "eligibility": current, "pending": pending,
                                     "trial_evidence": trial["routes"], "unsupported": trial["unsupported"]})


def _learner_trials(con, outcomes: list[dict]) -> dict:
    """What discovery trial dispatches taught the learner, apart from trust. A trial route's evidence is
    quality evidence only: its adapter trust is read here beside it and is never changed by it. Counted per
    trial dispatch, so a later retry that landed on the same route is not credited to the trial."""
    from office import scoring
    routes: dict[str, dict] = {}
    triples = {a["learner_route"]: a["route"] for a in route_learning.trial_attempts(con).values()}
    for o in outcomes:
        if not o.get("trial"):
            continue
        b = routes.setdefault(o["route"], {"dispatches": 0, "landed": 0, "not_the_model": 0, "failed_on_route": 0})
        b["dispatches"] += 1
        if o["success"]:
            b["landed"] += 1
        elif o["attribution"] in ("environment", "plan", "reviewer"):
            b["not_the_model"] += 1
        else:
            b["failed_on_route"] += 1
    unsupported = route_learning.unsupported_routes(con)
    trust_ready = route_learning._table(con, "adapter_trust_acts")
    lines = []
    if routes or unsupported:
        lines.append("trial evidence (quality only; trials never change adapter trust):")
    for route, b in sorted(routes.items()):
        trust = scoring.evaluate_trust_state(con, triples[route])[1] if route in triples and trust_ready else "unknown"
        b["trust"] = trust
        lines.append(f"  {route:<44} {b['landed']}/{b['dispatches']} trial dispatches landed"
                     + (f" | {b['not_the_model']} not the model's (launch, environment, quota or brief)" if b["not_the_model"] else "")
                     + (f" | {b['failed_on_route']} failed on the route" if b["failed_on_route"] else "")
                     + f" | trust {trust}")
    for route, ev in sorted(unsupported.items()):
        lines.append(f"  unsupported {route} (this exact effort only; attempt {ev['attempt_id']})")
    return {"lines": lines, "routes": routes, "unsupported": unsupported}
