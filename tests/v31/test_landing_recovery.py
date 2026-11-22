"""#1312: close --landed-externally carries the PR-scoped selector's integration attestation.

scripts/validate.sh --pr diffs HEAD against its merge-base with the default
branch. On the merged default-branch commit that diff is empty, so the gate
fails closed by design and can never be re-run there.
"""
from __future__ import annotations

from office import landing_recovery, state, verification_coverage
from test_issue_705_squash_close import _interrupted, _run_row


def _commands(env) -> list[str]:
    con = env.con()
    run = _run_row(env)
    snapshot = landing_recovery._snapshot(con, run)
    return list(dict.fromkeys([*snapshot["run_checks"],
                               *(c for t in state.tasks(con, run["id"]) for c in t["checks"])]))


def _attest(monkeypatch, scoped: list[str]) -> list[tuple[str, str]]:
    calls = []

    def attested(con, run, commit):
        calls.append(("attested", commit))
        return list(scoped)

    def scoped_validate(repo, commit, command):
        calls.append(("scoped", commit))
        return command in scoped
    monkeypatch.setattr(verification_coverage, "attested_checks", attested)
    monkeypatch.setattr(verification_coverage, "_scoped_validate", scoped_validate)
    return calls


def test_attested_scoped_check_is_carried_and_others_still_rerun_on_the_merged_commit(env, monkeypatch):
    _interrupted(env, monkeypatch)
    commands = _commands(env)
    assert len(commands) == 2, commands
    scoped, other = commands
    integrated = _run_row(env)["landing"]["integration"]["commit"]
    calls = _attest(monkeypatch, [scoped])
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/2")
    assert code == 0, out
    assert "scoped PR check carried from integration" in out, out
    proof = _run_row(env)["landing"]["squash_proof"]
    checks = proof["checks"]
    assert checks["verdict"] == "PASS"
    assert [r["command"] for r in checks["results"]] == [other]
    assert checks["carried"] == {scoped: {"scope": verification_coverage.SCOPED,
                                          "integration_commit": integrated, "gate": None}}
    # The attestation is read for the reviewed integration commit, never the merge commit.
    assert {c for _, c in calls} == {integrated}


def test_every_check_carried_runs_no_job_and_still_closes(env, monkeypatch):
    _interrupted(env, monkeypatch)
    _attest(monkeypatch, _commands(env))
    before = env.con().execute("SELECT count(*) FROM outbox WHERE kind='landing_verify'").fetchone()[0]
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/2")
    assert code == 0, out
    checks = _run_row(env)["landing"]["squash_proof"]["checks"]
    assert checks["results"] == [] and len(checks["carried"]) == 2
    assert env.con().execute("SELECT count(*) FROM outbox WHERE kind='landing_verify'").fetchone()[0] == before


def test_an_unattested_scoped_check_is_not_carried_and_reruns(env, monkeypatch):
    _interrupted(env, monkeypatch)
    commands = _commands(env)
    monkeypatch.setattr(verification_coverage, "attested_checks", lambda con, run, commit: [])
    monkeypatch.setattr(verification_coverage, "_scoped_validate", lambda repo, commit, command: True)
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/2")
    assert code == 0, out
    checks = _run_row(env)["landing"]["squash_proof"]["checks"]
    assert "carried" not in checks
    assert [r["command"] for r in checks["results"]] == commands


def test_a_scoped_check_on_an_unmerged_pr_still_refuses(env, monkeypatch):
    import json
    from test_task_prs import gh
    _interrupted(env, monkeypatch)
    _attest(monkeypatch, _commands(env))
    s = gh(env)
    s["prs"][1]["state"] = "open"
    (env.tmp / "gh.json").write_text(json.dumps(s))
    code, out = env.office("close", "--landed-externally", "https://github.com/o/r/pull/2")
    assert code != 0, out
    assert _run_row(env)["phase"] != "closed"
