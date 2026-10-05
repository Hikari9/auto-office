"""office preflight: read-only verdicts (ready|fix|wait|stop) an executor checks before
office submit, the opt-in macOS sed shell guard, and the executor brief's tail."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from test_self_review_ledger import write_ledger

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _worker(env, tid="T1"):
    con = env.con()
    t = task_row(env, tid)
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    return {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": tid,
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}, Path(d["worktree"]), d


def _dispatched(env):
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt, d = _worker(env)
    (wt / "calc.py").write_text(GOOD_ADD)
    env.git("add", "calc.py", cwd=wt)
    env.git("-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qm", "calc", cwd=wt)  # the ledger names HEAD
    write_ledger(wt)  # the self-review ledger an executor with a non-empty diff owes
    return wenv, wt, d


def _set_task(env, **cols):
    con = env.con()
    con.execute("UPDATE tasks SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id='T1'", tuple(cols.values()))
    con.commit()


@pytest.mark.integration
@pytest.mark.approved
def test_ready_prints_the_sourced_submit_line_and_submit_follows(env):
    wenv, wt, d = _dispatched(env)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready"), out
    assert f"agent.env && office submit" in out and d["id"] in out, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0, out


@pytest.mark.integration
@pytest.mark.approved
def test_wrong_role_is_a_fix_naming_the_agent_env(env):
    wenv, wt, d = _dispatched(env)
    code, out = env.office("preflight", cwd=wt, env={**wenv, "OFFICE_ROLE": "reviewer"})
    assert code == 1 and out.startswith("PREFLIGHT fix"), out
    assert "OFFICE_ROLE=reviewer" in out and f"{d['id']}/agent.env && office submit" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_lost_lease_is_terminal_and_never_reacquired(env):
    wenv, wt, d = _dispatched(env)
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and out.startswith("PREFLIGHT stop") and "stop: lease-lost" in out, out
    assert "do not retry" in out, out
    con = env.con()
    lease = con.execute("SELECT revoked_at FROM leases WHERE id=?", (d["lease_id"],)).fetchone()
    assert lease["revoked_at"], "preflight must not reacquire a revoked lease"


@pytest.mark.integration
@pytest.mark.approved
def test_expired_lease_alone_stops_even_while_the_task_looks_active(env):
    wenv, wt, d = _dispatched(env)
    con = env.con()
    con.execute("UPDATE leases SET revoked_at='2026-01-01T00:00:00+00:00' WHERE id=?", (d["lease_id"],))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "stop: lease-lost" in out and "fix:" not in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_plan_defect_pause_waits_then_turns_ready_when_it_clears(env):
    wenv, wt, _ = _dispatched(env)
    prior = task_row(env)["status"]
    _set_task(env, status="paused", pause_reason="plan defect")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 75 and out.startswith("PREFLIGHT wait") and "plan defect" in out, out
    _set_task(env, status=prior, pause_reason=None)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready"), out


@pytest.mark.integration
@pytest.mark.approved
def test_other_pauses_and_blocks_stop(env):
    wenv, wt, _ = _dispatched(env)
    _set_task(env, status="blocked", pause_reason="worker ended (crash) without submitting")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "stop: blocked" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_fix_round_without_findings_stops_and_names_the_escalation(env, tmp_path):
    wenv, wt, d = _dispatched(env)
    con = env.con()
    run_id = d["run_id"]
    from office import paths
    pkt = paths.run_dir(run_id) / "dispatches" / d["id"] / "packet.json"
    data = json.loads(pkt.read_text())
    data["fix_of"] = "R1"
    pkt.write_text(json.dumps(data))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "no open findings or amendments" in out, out
    # The command it names is one that resolves the stop; `office prompt` records no findings.
    assert "office amend T1 --" in out and "office rerun T1 --fresh" in out and "office prompt" not in out, out
    con.execute("INSERT INTO findings (id, run_id, task_id, code, severity, location, summary, state, created_at) "
                "VALUES ('f1', ?, 'T1', 'F1', 'medium', 'calc.py:1', 'add() drops negatives', 'open', '2026-01-01')",
                (run_id,))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and "finding: F1 [medium] calc.py:1 add() drops negatives" in out, out


def _signals(env):
    return [dict(r) for r in env.con().execute("SELECT * FROM events WHERE kind='worker.signal' ORDER BY seq")]


@pytest.mark.approved
def test_contract_conflict_and_round_cap_stops_signal_the_orchestrator(env):
    from test_self_review_ledger import ledger_text
    wenv, wt, d = _dispatched(env)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    write_ledger(wt, ledger_text(head, findings=["FINDING high edge-cases calc.py:2 | add must reject ints | contract-conflict accept=1"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "contract-conflict" in out, out
    write_ledger(wt, ledger_text(head, rnd=3, findings=["FINDING medium edge-cases calc.py:9 | add skips zero | open"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "round 3 ended with a finding still open" in out, out
    events = _signals(env)
    assert len(events) == 2 and all(e["audience"] == "orchestrator" and e["dispatch_id"] == d["id"] for e in events), events
    assert "contract-conflict" in events[0]["summary"] and "round 3" in events[1]["summary"], events
    assert "office amend T1 --contract" in json.loads(events[0]["payload_json"])["next"]
    assert "office rerun T1 --fresh" in json.loads(events[1]["payload_json"])["next"]


@pytest.mark.approved
def test_a_fix_signals_only_after_the_round_cap(env):
    from test_self_review_ledger import ledger_text
    wenv, wt, d = _dispatched(env)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    write_ledger(wt, ledger_text(head, rnd=2, findings=["FINDING low edge-cases calc.py:9 | nit | open"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and _signals(env) == [], out
    write_ledger(wt, ledger_text(head, rnd=3, findings=["FINDING low edge-cases calc.py:9 | nit | open"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and len(_signals(env)) == 1 and "round cap spent" in _signals(env)[0]["summary"], out


@pytest.mark.approved
@pytest.mark.parametrize("path", ["stop", "fix-after-cap"])
def test_preflight_changes_nothing_in_the_worktree_and_only_adds_the_event(env, path):
    from test_self_review_ledger import ledger_text
    wenv, wt, d = _dispatched(env)
    if path == "stop":
        env.office("revoke", "T1", env=EXTERNAL, check=0)
    else:
        head = env.git("rev-parse", "HEAD", cwd=wt).strip()
        write_ledger(wt, ledger_text(head, rnd=3, findings=["FINDING low edge-cases calc.py:9 | nit | open"]))

    def snapshot():
        files = {str(p.relative_to(wt)): p.read_bytes() for p in sorted(wt.rglob("*"))
                 if p.is_file() and ".git" not in p.relative_to(wt).parts}
        return files, env.git("status", "--porcelain", "--ignored", cwd=wt), env.git("rev-parse", "HEAD", cwd=wt)

    def tables():
        con = env.con()
        return {t: [tuple(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1")]
                for t in ("tasks", "dispatches", "leases", "revisions", "deliveries", "findings", "gates")}

    before_fs, before_db = snapshot(), tables()
    count = len(env.con().execute("SELECT 1 FROM events").fetchall())
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == (4 if path == "stop" else 1), out
    assert snapshot() == before_fs and tables() == before_db
    added = env.con().execute("SELECT kind FROM events ORDER BY seq").fetchall()[count:]
    assert [r[0] for r in added] and set(r[0] for r in added) == {"worker.signal"}, added


@pytest.mark.integration
@pytest.mark.approved
def test_a_dispatch_of_another_run_is_not_this_worker_and_signals_nothing(env):
    wenv, wt, d = _dispatched(env)
    con = env.con()
    con.execute("UPDATE dispatches SET run_id='some-other-run' WHERE id=?", (d["id"],))
    con.commit()
    code, out = env.office("preflight", cwd=env.repo, env={**wenv, "OFFICE_RUN_ID": d["run_id"]})
    assert code == 4 and "no executor dispatch owns this directory" in out, out
    assert _signals(env) == []


@pytest.mark.approved
def test_a_superseded_session_is_signalled_without_a_command_that_would_hit_the_holder(env):
    wenv, wt, d = _dispatched(env)
    _set_task(env, current_dispatch_id="Dnewer")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and "stop: superseded" in out, out
    nxt = json.loads(_signals(env)[0]["payload_json"])["next"]
    assert "revoke" not in nxt and "rerun" not in nxt, nxt
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 4, out
    nxt = json.loads(_signals(env)[-1]["payload_json"])["next"]
    assert "revoke" not in nxt and "rerun" not in nxt, nxt


@pytest.mark.approved
def test_a_renamed_out_of_scope_file_is_still_outside_scope(env):
    wenv, wt, d = _dispatched(env)
    env.git("mv", "README.md", "calc_notes.md", cwd=wt)  # destination is outside SCOPE too, source was tracked
    env.git("-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qam", "rename", cwd=wt)
    write_ledger(wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert "README.md" in out and "calc_notes.md" in out and "tracked edits outside SCOPE" in out, out


@pytest.mark.approved
def test_out_of_scope_edit_and_bsd_sed_backup_are_fixes(env):
    wenv, wt, d = _dispatched(env)
    (wt / "README.md").write_text("stamped by a hook\n")
    env.git("add", "-N", "README.md", cwd=wt)
    env.git("add", "calc.py", cwd=wt)
    (wt / "calc.py-e").write_text(GOOD_ADD)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and out.startswith("PREFLIGHT fix"), out
    assert "scope: tracked edits outside SCOPE: README.md" in out and "--request-scope" in out, out
    assert "sed: BSD sed wrote backup files calc.py-e" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_dispatch_env_from_another_directory_is_a_worktree_fix(env):
    wenv, wt, _ = _dispatched(env)
    code, out = env.office("preflight", cwd=env.repo, env=wenv)
    assert code == 1 and f"fix: worktree: submit refuses outside the task worktree; cd {wt.resolve()}" in out, out


@pytest.mark.integration
def test_scope_none_stale_evidence_is_a_fix(env):
    from conftest import PLAN_ONE
    approved_run(env, plan=PLAN_ONE.replace("scope: calc.py", "scope: none"),
                 executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    wenv, wt, d = _worker(env)
    (wt / "OFFICE_EVIDENCE.md").write_text("posted comment https://example.test/c/1: hello\n")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0, out
    from office import submit
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    digest = submit._read_evidence(wt)[2]
    con.execute("INSERT INTO evidence (id, run_id, task_id, kind, sha256, created_at) VALUES ('e1', ?, 'T1', ?, ?, '2026-01-01')",
                (run["id"], submit.EVIDENCE_KIND, digest))
    con.commit()
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "fix: evidence: OFFICE_EVIDENCE.md repeats evidence already submitted" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_preflight_outside_a_task_worktree_stops(env):
    _dispatched(env)
    code, out = env.office("preflight", cwd=env.repo, env=EXTERNAL)
    assert code == 4 and "no executor dispatch owns this directory" in out, out


@pytest.mark.integration
@pytest.mark.approved
def test_executor_brief_carries_self_review_preflight_and_status_line(env):
    _, _, d = _dispatched(env)
    from office import paths
    brief = (paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "brief.md").read_text()
    for lens in ("(a) security", "(b) edge cases", "(c) platform and build", "(d) test strength"):
        assert lens in brief, lens
    assert f"git diff {d['base_commit']}" in brief
    assert "office preflight" in brief and "lease-lost, superseded-dispatch, or task-paused is terminal" in brief
    last = brief.rstrip().splitlines()[-1].strip()
    assert last.startswith("TASK=<id> COMMIT=<sha> PUSHED=") and "SUBMIT=<accepted Rn | refused: exact reason" in last



def _tail_order(brief: str) -> tuple[int, int, int]:
    return (brief.index("SIMPLIFY after targeted checks"), brief.index("SELF-REVIEW before submitting"),
            brief.index("WHEN DONE run: office preflight"))


@pytest.mark.integration
@pytest.mark.approved
def test_executor_brief_simplifies_before_self_review_and_preflight(env):
    _, _, d = _dispatched(env)
    from office import paths
    brief = (paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "brief.md").read_text()
    s, r, w = _tail_order(brief)
    assert s < r < w
    simplify = brief[s:r]
    assert f"git diff {d['base_commit']}" in simplify
    for lens in ("(a) reuse", "(b) simplification", "(c) efficiency", "(d) altitude"):
        assert lens in simplify, lens
    for rule in ("Behavior-preserving only", "auth, validation, migrations, SQL", "data semantics",
                 "only if it is inside SCOPE", "outside it goes in your report, unedited",
                 "tiny mechanical diff", "rerun it after any non-trivial repair", "no other writer"):
        assert rule in simplify, rule
    assert "SIMPLIFY" not in brief[w:].split("FINAL REPORT")[0]
    assert "SIMPLIFY opportunity outside SCOPE" in brief[brief.index("FINAL REPORT"):]


@pytest.mark.integration
@pytest.mark.approved
def test_fix_round_brief_repeats_simplify_self_review_preflight_tail(env):
    _, _, d = _dispatched(env)
    from office import briefs, paths
    con = env.con()
    con.execute("INSERT INTO findings (id, run_id, task_id, code, severity, location, summary, state, created_at) "
                "VALUES ('f1', ?, 'T1', 'F1', 'medium', 'calc.py:1', 'add() drops negatives', 'open', '2026-01-01')",
                (d["run_id"],))
    con.commit()
    data = json.loads((paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "packet.json").read_text())
    data["fix_of"] = "R1"
    run = dict(con.execute("SELECT * FROM runs WHERE id=?", (d["run_id"],)).fetchone())
    brief = briefs.executor_brief(con, run, data)
    s, r, w = _tail_order(brief)
    assert brief.index("FIX ROUND for revision R1") < s < r < w


# ------------------------------------------------------------------ self-review tier

GEARS = ("direct", "direct+review", "light", "quick", "express", "full")
LOW_GEARS = ("direct", "direct+review", "light")


def _risk(blast, irreversible=False, size=None, high=False):
    return json.dumps({"blast_radius": blast, "size_class": size, "irreversible": irreversible, "high": high})


def _expected_tier(gear, blast):
    if gear == "full" or blast in ("production", "production-data"):
        return "deep"
    if gear in LOW_GEARS and blast in ("local", "repo"):
        return "inline"
    return "single"


@pytest.mark.parametrize("gear", GEARS)
@pytest.mark.parametrize("blast", ["local", "repo", "production", "production-data", None])
def test_tier_table_by_gear_and_blast_radius(gear, blast):
    from office import briefs
    from office.config import resolve_risk
    risk = json.dumps(resolve_risk({}, blast, None, False))
    tier = briefs.self_review_tier(gear, risk)
    assert tier == _expected_tier(gear, blast), (gear, blast, tier)
    if blast is None:
        assert tier != "inline"


@pytest.mark.parametrize("gear", GEARS)
@pytest.mark.parametrize("blast", ["local", "repo", None])
def test_irreversible_size_l_and_high_are_deep_in_every_gear(gear, blast):
    from office import briefs
    assert briefs.self_review_tier(gear, _risk(blast, irreversible=True)) == "deep"
    assert briefs.self_review_tier(gear, _risk(blast, size="L")) == "deep"
    assert briefs.self_review_tier(gear, _risk(blast, size="XL")) == "deep"
    assert briefs.self_review_tier(gear, _risk(blast, high=True)) == "deep"


@pytest.mark.parametrize("blast", ["local", "repo", "production", None])
def test_gear_full_is_deep_even_without_a_usable_risk_record(blast):
    from office import briefs
    assert briefs.self_review_tier("full", _risk(blast)) == "deep"
    assert briefs.self_review_tier("full", None) == "deep"
    assert briefs.self_review_tier("full", "{not json") == "deep"


@pytest.mark.parametrize("risk_json", [None, "", "{not json", "null", "[]", "42", '"local"', b"\xff", {}])
@pytest.mark.parametrize("gear", [*GEARS, None, "", "turbo"])
def test_missing_or_unparsable_risk_never_yields_inline(gear, risk_json):
    from office import briefs
    tier = briefs.self_review_tier(gear, risk_json)
    assert tier == ("deep" if gear == "full" else "single"), (gear, risk_json, tier)


def test_pathologically_nested_risk_record_fails_toward_review():
    from office import briefs
    assert briefs.self_review_tier("direct", "[" * 500_000) == "single"
    assert briefs.self_review_tier("full", "[" * 500_000) == "deep"


@pytest.mark.parametrize("gear", [None, "", "turbo", "Direct", 7])
def test_unknown_gear_is_never_inline_even_for_a_local_risk(gear):
    from office import briefs
    assert briefs.self_review_tier(gear, _risk("local")) == "single"


@pytest.mark.parametrize("blast", ["", "LOCAL", "weird", ["local"], 3])
def test_unrecognized_blast_radius_is_not_inline(blast):
    from office import briefs
    assert briefs.self_review_tier("direct", _risk(blast)) == "single"


def _self_review_block(brief: str) -> str:
    return brief[brief.index("SELF-REVIEW before submitting"):brief.index("WHEN DONE run: office preflight")]


def _tier_brief(tier: str) -> str:
    from office import briefs
    gear, risk = {"inline": ("direct", _risk("local")), "single": ("quick", _risk("local")),
                  "deep": ("full", _risk("production"))}[tier]
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1,
              "base_commit": "abc123"}
    brief = briefs.executor_brief(None, {"gear": gear, "risk_json": risk}, packet)
    assert f"(tier: {tier})" in brief
    return _self_review_block(brief)


@pytest.mark.parametrize("tier", ["inline", "single", "deep"])
def test_every_tier_keeps_lenses_json_shape_fix_rule_and_mutation_proof(tier):
    block = _tier_brief(tier)
    for lens in ("(a) security", "(b) edge cases", "(c) platform and build", "(d) test strength"):
        assert lens in block, lens
    assert '[{"severity": "high|medium|low", "location": "file:line", "repro": "...", "fix": "..."}]' in block
    assert "Fix every medium or higher finding inside SCOPE" in block
    assert "revert the fix, confirm the test fails, restore it" in block
    assert "git diff abc123" in block
    assert "A finding outside SCOPE goes in your report, unfixed" in block


def test_inline_tier_has_no_subagent_instruction_and_the_shared_round_rules():
    block = _tier_brief("inline")
    assert "subagent" not in block.lower()
    assert "fix-diff re-review" in block and "3-round cap" in block
    assert "one fresh pass per lens yourself" in block
    assert "You may skip a lens that clearly does not\n    apply, with a one-line reason in your report" in block


def test_inline_tier_applies_to_repo_blast_but_not_to_quick_gear():
    from office import briefs
    assert briefs.self_review_tier("direct", _risk("repo")) == "inline"
    assert briefs.self_review_tier("light", _risk("repo")) == "inline"
    assert briefs.self_review_tier("quick", _risk("local")) == "single"
    assert briefs.self_review_tier("quick", _risk("repo")) == "single"
    assert briefs.self_review_tier("direct", _risk("repo", high=True)) == "deep"
    assert briefs.self_review_tier("direct", _risk(None)) == "single"


def test_single_tier_names_exactly_one_subagent_covering_all_four_lenses():
    block = _tier_brief("single")
    assert "Start exactly one subagent" in block and "all four" in block
    assert "four parallel subagents" not in block
    assert block.lower().count("subagent") == 1
    assert "3-round cap" in block and "skip a lens" not in block


def test_deep_tier_keeps_four_parallel_subagents_and_three_rounds():
    block = _tier_brief("deep")
    assert "Start four parallel subagents" in block
    assert "each given only the diff and one\n    lens" in block and "3-round cap" in block
    assert "exactly one subagent" not in block


@pytest.mark.parametrize("injected", ["inline", "single", "deep", "none", True])
def test_packet_cannot_lower_the_tier(injected):
    from office import briefs
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1,
              "self_review_tier": injected, "tier": injected, "gear": "direct", "risk": {"blast_radius": "local"},
              "risk_json": _risk("local"), "blast_radius": "local"}
    deep = briefs.executor_brief(None, {"gear": "full", "risk_json": _risk("production")}, packet)
    assert "(tier: deep)" in deep
    unset = briefs.executor_brief(None, {"gear": "direct", "risk_json": _risk(None)}, packet)
    assert "(tier: single)" in unset
    assert "(tier: single)" in briefs.executor_brief(None, {}, packet)


def _tier_of(brief: str) -> str:
    import re
    return re.search(r"SELF-REVIEW before submitting \(tier: (\w+)\)", brief).group(1)


@pytest.mark.integration
@pytest.mark.approved
@pytest.mark.parametrize("gear, blast, tier", [("direct", "local", "inline"), ("direct", "repo", "inline"),
                                               ("quick", "local", "single"), ("express", "repo", "single"),
                                               ("full", "production", "deep")])
def test_fix_round_brief_prints_the_same_tier_as_the_initial_brief(env, gear, blast, tier):
    _, _, d = _dispatched(env)
    from office import briefs, paths
    con = env.con()
    con.execute("UPDATE runs SET gear=?, risk_json=? WHERE id=?", (gear, _risk(blast), d["run_id"]))
    con.execute("INSERT INTO findings (id, run_id, task_id, code, severity, location, summary, state, created_at) "
                "VALUES ('f1', ?, 'T1', 'F1', 'medium', 'calc.py:1', 'add() drops negatives', 'open', '2026-01-01')",
                (d["run_id"],))
    con.commit()
    data = json.loads((paths.run_dir(d["run_id"]) / "dispatches" / d["id"] / "packet.json").read_text())
    run = dict(con.execute("SELECT * FROM runs WHERE id=?", (d["run_id"],)).fetchone())
    initial = briefs.executor_brief(con, run, {**data, "fix_of": None})
    fix = briefs.executor_brief(con, run, {**data, "fix_of": "R1"})
    assert "FIX ROUND for revision R1" in fix and "FIX ROUND" not in initial
    assert _tier_of(initial) == _tier_of(fix) == tier


def test_office_submit_skill_simplifies_before_self_review():
    text = (Path(__file__).resolve().parents[2] / "skills/office-submit/SKILL.md").read_text()
    order = [text.index(h) for h in ("## 1. Simplify", "## 2. Adversarial self-review", "## 3. Checks",
                                     "## 5. Preflight", "## 6. Submit", "## 7. Report")]
    assert order == sorted(order)
    simplify = text[order[0]:order[1]]
    for rule in ("Behavior-preserving only", "outside SCOPE goes in the report, unedited", "tiny mechanical diff",
                 "yourself", "Fix rounds repeat"):
        assert rule in simplify, rule

# ------------------------------------------------------------------ shell guard

@pytest.mark.parametrize("cmd, want", [
    ("sed -i 's/a/b/' f", "sed -i '' 's/a/b/' f"),
    ("sed -i -e 's/a/b/' f", "sed -i '' -e 's/a/b/' f"),
    ("sed -E -i 's/(a)/b/' f", "sed -E -i '' 's/(a)/b/' f"),
    ("sed -Ei 's/a/b/' f", "sed -Ei '' 's/a/b/' f"),
    ("find . -name x | xargs sed -i 's/x/y/'", "find . -name x | xargs sed -i '' 's/x/y/'"),
    ("echo ok && sed -i s/a/b/ f && sed -i s/c/d/ g", "echo ok && sed -i '' s/a/b/ f && sed -i '' s/c/d/ g"),
    ("sed -i '' 's/a/b/' f", None),
    ("sed -i \"\" 's/a/b/' f", None),
    ("sed -i.bak 's/a/b/' f", None),
    ("gsed -i 's/a/b/' f", None),
    ("sed -n 1p f", None),
    ('git commit -m "replace sed -i with perl"', None),
    ("cat > fix.sh <<'EOF'\nsed -i 's/a/b/' f\nEOF", None),
    ("cat > fix.sh <<EOF\nsed -i 's/a/b/' f\nEOF", None),
    ("sed -i 's/a/b/' f && cat <<EOF\nsed -i s/x/y/ g\nEOF", "sed -i '' 's/a/b/' f && cat <<EOF\nsed -i s/x/y/ g\nEOF"),
])
def test_portable_sed(cmd, want):
    from office.hooks import portable_sed
    assert portable_sed(cmd) == want


def _guard(monkeypatch, capsys, command, platform="darwin", gnu=False):
    import io
    from office import hooks
    monkeypatch.setattr(hooks.sys, "platform", platform)
    monkeypatch.setattr(hooks, "_gnu_sed", lambda: gnu)
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO(json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command, "description": "d"}})))
    assert hooks.main(["shell.pre", "--harness", "claude", "--office-managed"]) == 0
    return capsys.readouterr().out


def test_shell_guard_rewrites_without_a_permission_decision(monkeypatch, capsys):
    out = json.loads(_guard(monkeypatch, capsys, "sed -i 's/a/b/' f"))["hookSpecificOutput"]
    assert out["updatedInput"] == {"command": "sed -i '' 's/a/b/' f", "description": "d"}
    assert "permissionDecision" not in out, "the rewrite must stay inside the normal permission flow"


@pytest.mark.parametrize("kw", [{"platform": "linux"}, {"gnu": True}])
def test_shell_guard_is_silent_off_macos_and_with_gnu_sed(monkeypatch, capsys, kw):
    assert _guard(monkeypatch, capsys, "sed -i 's/a/b/' f", **kw) == ""


@pytest.mark.integration
def test_install_shell_guard_is_opt_in_kept_and_removable(env, tmp_path, monkeypatch):
    from office import install
    cfg = tmp_path / "settings.json"
    cfg.write_text("{}")
    monkeypatch.setitem(install.CONFIG, "claude", cfg)
    monkeypatch.setitem(install.CONFIG, "gemini", tmp_path / "absent" / "settings.json")
    monkeypatch.setattr(install.read_scope, "sync", lambda data, ledger: ([], [], []))
    monkeypatch.setattr(install.read_scope, "load_ledger", lambda: [])

    def guards():
        pre = json.loads(cfg.read_text())["hooks"]["PreToolUse"]
        return [e for e in pre if install._is_managed(e, "shell.pre")], [e for e in pre if install._is_managed(e, "tool.pre")]

    install.install(only=["claude"], dry_run=True)  # no file write in dry run
    res = install.install(only=["claude"], dry_run=False)  # noqa: F841
    assert guards()[0] == [] and len(guards()[1]) == 1
    install.install(only=["claude"], shell_guard=True)
    g, w = guards()
    assert len(g) == 1 and g[0]["matcher"] == "Bash" and len(w) == 1
    res = install.install(only=["claude"])  # plain reinstall keeps it
    assert len(guards()[0]) == 1 and any("hooks already current" in l for l in res.lines), res.lines
    install.install(only=["claude"], shell_guard=False)
    assert guards()[0] == [] and len(guards()[1]) == 1


def test_office_submit_skill_reads_the_tier_and_no_longer_requires_four_subagents_unconditionally():
    text = (Path(__file__).resolve().parents[2] / "skills/office-submit/SKILL.md").read_text()
    step = text[text.index("## 2. Adversarial self-review"):text.index("## 3. Checks")]
    assert "`SELF-REVIEW` line" in step and "(tier: <tier>)" in step
    for tier in ("`inline`", "`single`", "`deep`"):
        assert tier in step, tier
    inline = step[step.index("**`inline`:**"):step.index("**`single`:**")]
    single = step[step.index("**`single`:**"):step.index("**`deep`:**")]
    deep = step[step.index("**`deep`:**"):]
    assert "no subagents" in inline and "Agent" not in inline
    assert "skip a lens that clearly does not apply" in inline and "one-line reason" in inline
    assert "exactly one `Agent` subagent" in single and "all four lenses" in single
    assert "four `Agent` subagents" in deep
    assert "3-round cap" in step and "fix-diff re-review" in step
    assert "four `Agent` subagents" not in step.replace(deep.split("\n\n")[0], "")
    assert "You cannot lower it" in step
