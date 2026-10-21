def test_recovery_hint_offers_fresh_only_after_invocation_drift(env, monkeypatch):
    from office import candidates, rerun, state
    from test_issue_707_route_authority import FB1, _approve, _ended, _latest, _quota

    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, "--quote", "Use Astra.",
               env=_quota(env), check=0)
    _ended(env)
    con = env.con()
    con.execute("UPDATE dispatches SET session_id='valid-session' WHERE id=?", (_latest(env)["id"],))
    con.commit()
    parent = state.get_dispatch(con, _latest(env)["id"])
    run = state.get_run(con, parent["run_id"])

    real = candidates.catalog_rows

    def changed_invocation():
        return [{**row, "invocation_model_id": "gpt-6-astra-new-invocation"}
                if row.get("model_id") == "gpt-6-astra" else row for row in real()]

    monkeypatch.setattr(candidates, "catalog_rows", changed_invocation)
    hint = rerun.recovery_hint(con, run, "T1")
    assert hint == "office rerun T1 --fresh", hint
    code, out = env.office("rerun", "T1", "--resume", env=_quota(env))
    assert code == 4 and "resume-impossible" in out, out
    assert out.splitlines()[-1] == "next: office rerun T1 --fresh", out
    env.office("rerun", "T1", "--fresh", env=_quota(env), check=0)
    assert _latest(env)["model"] == "gpt-6-astra-new-invocation"


def test_recovery_hint_uses_current_quote_when_route_returns_to_parent_triple(env):
    from office import rerun, state
    from test_adaptive_dispatch import PRIMARY
    from test_issue_707_route_authority import FB1, _approve, _ended, _latest, _quota

    _approve(env)
    env.office("dispatch", "T1", "--as", FB1, env=_quota(env), check=0)
    _ended(env)
    env.office("amend", "route", "T1", "--as", PRIMARY, "--quote", "Use Claude for the next turn.",
               env=_quota(env), check=0)
    env.office("amend", "route", "T1", "--as", FB1, "--quote", "Use Astra for the next turn.",
               env=_quota(env), check=0)
    con = env.con()
    parent = state.get_dispatch(con, _latest(env)["id"])
    run = state.get_run(con, parent["run_id"])
    con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
    con.commit()

    hint = rerun.recovery_hint(con, run, "T1")
    assert hint == "office rerun T1 --fresh", hint
    env.office("rerun", "T1", "--fresh", env=_quota(env), check=0)
    assert _latest(env)["triple"] == parent["triple"]
