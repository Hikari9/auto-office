"""A named executor route has user authority only with the user's actual words."""
import json
import sqlite3

import pytest

from test_adaptive_dispatch import FB1, _approve, _quota


def _latest(env):
    return dict(env.con().execute(
        "SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC LIMIT 1").fetchone())


def _ended(env):
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at=started_at WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()


def _events(env):
    return [json.loads(r[0]) for r in env.con().execute(
        "SELECT payload_json FROM events WHERE kind='route.changed' ORDER BY seq")]


@pytest.mark.parametrize("command", ["dispatch", "rerun"])
def test_unquoted_as_records_orchestrator_authority_and_can_be_rerouted(env, command):
    _approve(env)
    if command == "rerun":
        env.office("dispatch", "T1", env=_quota(env), check=0)
        _ended(env)
    args = [command, "T1", *(["--fresh"] if command == "rerun" else []), "--as", FB1]
    code, out = env.office(*args, env=_quota(env))
    assert code == 0 and "user override" not in out, out
    d = _latest(env)
    route = json.loads(d["route_json"])
    assert not route.get("override") and not route.get("declared"), route
    assert route["route_authority"]["actor"] == "orchestrator"
    assert route["route_authority"]["reason"]
    assert _events(env)[-1]["actor"] == "orchestrator"
    assert "user override" not in _events(env)[-1]["reason"]
    # The named route becomes unavailable; an ordinary evidence reroute can replace it.
    _ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--reroute", env=_quota(env, codex=1))
    assert code == 0 and _latest(env)["triple"] != d["triple"], out
    assert _events(env)[-1]["actor"] == "orchestrator"


@pytest.mark.parametrize("flag", ["--as", "--route"])
def test_unquoted_named_route_cannot_spend_protected_quota(env, flag):
    _approve(env)
    code, out = env.office("dispatch", "T1", flag, FB1, env=_quota(env, codex=1))
    assert code == 4 and "quota" in out.lower(), out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["dispatch", "rerun"])
def test_quoted_route_keeps_verbatim_authority_through_rerun(env, command):
    _approve(env)
    if command == "rerun":
        env.office("dispatch", "T1", env=_quota(env), check=0)
        _ended(env)
    quote = "Use Astra for this task; I authorize that route."
    args = [command, "T1", *(["--fresh"] if command == "rerun" else []),
            "--as", FB1, "--quote", quote]
    code, out = env.office(*args, env=_quota(env))
    assert code == 0 and "user override" in out, out
    d = _latest(env)
    route = json.loads(d["route_json"])
    assert route["override"] and route["declared"]
    assert route["route_authority"]["actor"] == "user" and route["route_authority"]["quote"] == quote
    assert json.loads(d["override_json"])["quote"] == quote
    assert _events(env)[-1]["actor"] == "user" and _events(env)[-1]["quote"] == quote
    _ended(env)
    env.office("rerun", "T1", "--fresh", env=_quota(env), check=0)
    inherited = json.loads(_latest(env)["route_json"])
    assert inherited["route_authority"] == route["route_authority"]


def test_unquoted_as_does_not_bypass_registry_or_trust(env):
    _approve(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/some-future-model@low", env=_quota(env))
    assert code == 4 and "no qualifying" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


def test_unquoted_known_route_does_not_need_historical_trust(env):
    _approve(env)
    con = env.con()
    con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
    con.commit()
    code, out = env.office("dispatch", "T1", "--as", FB1, env=_quota(env))
    assert code == 0 and "approve trust" not in out.lower(), out  # #837: no trust history, still neutral and eligible
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 1


@pytest.mark.parametrize("flags", [["--as", FB1, "--quote", "  "], ["--quote", "Use Astra"]])
def test_empty_or_unattached_quote_is_rejected(env, flags):
    _approve(env)
    code, out = env.office("dispatch", "T1", *flags, env=_quota(env))
    assert code == 2 and "quote" in out.lower(), out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


@pytest.mark.parametrize("still_trusted", [True, False])
def test_legacy_unquoted_override_is_not_inherited_as_user_authority(env, still_trusted):
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, "--quote", "fixture route", env=_quota(env), check=0)
    old = _latest(env)
    # Main before #707 stored this shape for --as with no user words. Keep
    # that historical producer immutable, but do not grant its next run consent.
    route = json.loads(old["route_json"])
    route.pop("route_authority", None)
    route.pop("route_change", None)
    route["selection_disclosure"]["reason"] = f"user override (--as {FB1})"
    override = json.loads(old["override_json"])
    override.pop("quote", None)
    con = env.con()
    con.execute("UPDATE dispatches SET route_json=?, override_json=? WHERE id=?",
                (json.dumps(route), json.dumps(override), old["id"]))
    con.execute("UPDATE tasks SET route_json=? WHERE id='T1'", (json.dumps(route),))
    if not still_trusted:
        con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
    con.commit()
    _ended(env)
    old = _latest(env)
    count = con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0]
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env))
    assert dict(con.execute("SELECT * FROM dispatches WHERE id=?", (old["id"],)).fetchone()) == old
    assert code == 0 and "user override" not in out, out  # #837: trust history is irrelevant either way
    inherited = json.loads(_latest(env)["route_json"])
    assert not inherited.get("override") and not inherited.get("declared"), inherited
    assert inherited["route_authority"]["actor"] == "orchestrator"
    assert not inherited["route_authority"].get("quote")


def test_unquoted_pin_cannot_rerun_when_every_route_would_spend_protected_quota(env):
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, env=_quota(env), check=0)
    _ended(env)
    count = env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0]
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env, codex=1, claude=1, agy=1, gemini=1))
    assert code == 4 and "quota" in out.lower(), out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == count


def test_route_keeps_model_only_selector_and_family_floor_exception_without_user_authority(env):
    (env.tmp / "user-config.yaml").write_text("model_family_floors:\n  gpt: '99'\n")
    _approve(env)
    code, out = env.office("dispatch", "T1", "--as", FB1, env=_quota(env))
    assert code == 4 and "family floor" in out, out
    code, out = env.office("dispatch", "T1", "--route", "gpt-6-astra@low", env=_quota(env))
    assert code == 0 and "user override" not in out, out
    chosen = _latest(env)["triple"]
    _ended(env)
    env.office("rerun", "T1", "--fresh", env=_quota(env), check=0)
    assert _latest(env)["triple"] == chosen
    assert json.loads(_latest(env)["route_json"])["route_authority"]["actor"] == "orchestrator"
    _ended(env)
    con = env.con()
    con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
    con.commit()
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env))
    assert code == 0 and "approve trust" not in out.lower(), out  # #837: no trust rows, still eligible


@pytest.mark.parametrize("schema_present", [False, True])
def test_ensuring_trust_schema_preserves_the_callers_atomic_rollback(schema_present):
    from office import db, scoring
    con = sqlite3.connect(":memory:", isolation_level=None)
    try:
        con.execute("CREATE TABLE pending_amendment(id TEXT)")
        if schema_present:
            scoring.ensure_trust_schema(con)
        with pytest.raises(RuntimeError, match="cancel amendment"):
            with db.transaction(con):
                con.execute("INSERT INTO pending_amendment VALUES('A1')")
                scoring.ensure_trust_schema(con)
                raise RuntimeError("cancel amendment")
        assert con.execute("SELECT COUNT(*) FROM pending_amendment").fetchone()[0] == 0
        assert not con.in_transaction
    finally:
        con.close()


def test_route_admission_ignores_trust_rows_and_rolls_back_with_its_caller(env):
    from office import db, dispatch, state
    from office.state import Refused
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, env=_quota(env), check=0)
    _ended(env)
    con = env.con()
    parent = state.get_dispatch(con, _latest(env)["id"])
    run = state.get_run(con, parent["run_id"])
    task = state.get_task(con, run["id"], "T1")
    before = con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple LIKE 'codex@%'").fetchone()[0]
    assert before > 0
    with pytest.raises(RuntimeError, match="roll back the caller"):
        with db.transaction(con):
            con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
            dispatch.qualify_recorded_route(con, run, task, parent["route"])  # no trust refusal (#837)
            raise RuntimeError("roll back the caller")
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple LIKE 'codex@%'").fetchone()[0] == before
    assert not con.in_transaction


def test_sticky_recovery_observation_uses_readonly_evidence_and_emits_no_notice(env, monkeypatch):
    from office import candidates, db, paths, rerun, state
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, env=_quota(env), check=0)
    _ended(env)
    con = env.con()
    parent = state.get_dispatch(con, _latest(env)["id"])
    run = state.get_run(con, parent["run_id"])
    task = state.get_task(con, run["id"], "T1")
    before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "on")
    monkeypatch.setattr(candidates, "probe_quota", lambda *a, **kw: {
        "status": "unknown", "tightest_remaining_percent": None, "cause": "fixture unavailable"})
    reader = sqlite3.connect(f"file:{paths.runs_db()}?mode=ro", uri=True, timeout=0.1)
    reader.row_factory = sqlite3.Row
    try:
        # Recovery asks from a read-only snapshot while its reconcile transaction
        # owns the writer. No second writable connection or quota notice is needed.
        with db.transaction(con):
            assert rerun._sticky_check(reader, run, task, parent) is None
        assert reader.execute("SELECT 1").fetchone()[0] == 1  # caller still owns it
    finally:
        reader.close()
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_sticky_recovery_observation_reports_unreadable_evidence_as_a_blocker(monkeypatch):
    from office import dispatch, rerun
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fixture evidence unavailable")
    monkeypatch.setattr(dispatch, "qualify_recorded_route", unavailable)
    assert "route evidence could not be read" in rerun._sticky_check(None, {}, {}, {"role": "executor"})


def test_derived_quarantine_of_the_current_harness_version_no_longer_blocks_a_quoted_rerun(env, monkeypatch):
    from office import adapters, scoring
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, "--quote", "Use Astra.", env=_quota(env), check=0)
    _ended(env)
    prior = _latest(env)
    con = env.con()
    current = "codex@2/gpt-6-astra@low"
    con.execute("INSERT INTO dispatches(id, run_id, role, kind, triple, attribution, status) "
                "VALUES('Dfailed-adapter', ?, 'executor', 'executor', ?, 'adapter', 'ended')",
                (prior["run_id"], current))
    con.execute("INSERT INTO outcome_labels(id, dispatch_id, label, primary_attribution, evidence_hash) "
                "VALUES('Ofailed-adapter', 'Dfailed-adapter', 'recurrence_failure', 'adapter', ?)",
                ("sha256:" + "a" * 64,))
    assert scoring.evaluate_trust_state(con, current)[1] == "quarantined"
    real = adapters.route_version
    monkeypatch.setattr(adapters, "route_version", lambda harness, *args, **kwargs:
                        "2.0.0" if harness == "codex" else real(harness, *args, **kwargs))
    before = con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env))
    assert code == 0 and "quarantined" not in out, out  # #837: derived trust is audit evidence, not a gate
    assert con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == before + 1
    assert dict(con.execute("SELECT * FROM dispatches WHERE id=?", (prior["id"],)).fetchone()) == prior


def test_resume_cannot_change_the_invocation_of_an_inherited_quoted_route(env, monkeypatch):
    from office import candidates
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, "--quote", "Use Astra.", env=_quota(env), check=0)
    _ended(env)
    prior = _latest(env)
    real = candidates.catalog_rows
    def changed_invocation():
        return [{**row, "invocation_model_id": "gpt-6-astra-new-invocation"}
                if row.get("invocation_harness") == "codex" and row.get("model_id") == "gpt-6-astra" else row
                for row in real()]
    monkeypatch.setattr(candidates, "catalog_rows", changed_invocation)
    code, out = env.office("rerun", "T1", "--resume", env=_quota(env))
    assert code == 4 and "keeps its harness, model and effort" in out and "--fresh" in out, out
    assert _latest(env) == prior
    env.office("rerun", "T1", "--fresh", env=_quota(env), check=0)
    assert _latest(env)["model"] == "gpt-6-astra-new-invocation"
    assert dict(env.con().execute("SELECT * FROM dispatches WHERE id=?", (prior["id"],)).fetchone()) == prior


def test_quoted_relaunch_reads_the_runs_repo_denial_from_outside_that_repo(env, monkeypatch):
    from office import dispatch, state
    from office.state import Refused
    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, "--quote", "Use Astra.", env=_quota(env), check=0)
    _ended(env)
    con = env.con()
    parent = state.get_dispatch(con, _latest(env)["id"])
    run = state.get_run(con, parent["run_id"])
    task = state.get_task(con, run["id"], "T1")
    config = env.repo / ".auto-office" / "config.yaml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(f"routing:\n  user_policy:\n    denied_models: [{FB1}]\n")
    monkeypatch.chdir(env.tmp)
    with pytest.raises(Refused, match="denied"):
        dispatch.qualify_recorded_route(con, run, task, parent["route"])
