"""H4 (#1178): agy reviews code only as the exact user-allowed Hydrogen@high route.

The shipped agy adapter declares a read-only (`--mode plan`) reviewer profile scoped by `routes` to
gemini-4-hydrogen-high@high. These tests run the real seed adapter and catalog rows through candidate
building, routing, argv rendering and `office route policy`, not a synthetic candidate.
"""
from pathlib import Path

import pytest

from office import adapters, candidates, db, hydrogen_trials, route_policy, route_probe, routecmd, routing
from office.util import now_iso

HYDROGEN = "agy/gemini-4-hydrogen@high"
ALLOW = "agy@1/gemini-4-hydrogen@high"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [])
    monkeypatch.setattr(adapters, "installed", lambda _a: True)
    monkeypatch.setattr(adapters, "route_version", lambda *_a: "1.2.3")
    return tmp_path


def _config(*routes):
    return {"routing": {"user_policy": {"role_rules": [
        {"role": "code_reviewer", "route": r, "state": "allow"} for r in routes]}}}


def _agy():
    return adapters.load_all()["agy"]


def _labels(entries):
    return {e["candidate"]: e for e in entries}


def test_seed_reviewer_profile_is_read_only_and_scoped():
    prof = adapters.profile(_agy(), "reviewer")
    assert prof["routes"] == ["gemini-4-hydrogen-high@high"]
    assert prof["argv"][:2] == ["--mode", "plan"]
    assert prof["verify_worktree_unchanged"] is True
    assert not any("dangerously" in str(a) for a in prof["argv"])
    assert adapters.pane_capable(_agy(), "reviewer")
    pane = prof["interactive"]["argv"]
    assert pane[:2] == ["--mode", "plan"]
    assert not any("dangerously" in str(a) or a in ("auto", "accept-edits") for a in pane)


def test_profile_for_opens_only_the_hydrogen_route():
    agy = _agy()
    assert adapters.profile_for(agy, "reviewer", "gemini-4-hydrogen-high", "high")
    for model, effort in [("gemini-4-hydrogen-medium", "medium"), ("gemini-4-hydrogen-high", "medium"),
                          ("Claude Opus 5.5 (Low)", "low"), ("gemini-3.8-flash-medium", "medium"),
                          ("gemini-4-argon-high", "high")]:
        assert adapters.profile_for(agy, "reviewer", model, effort) is None, (model, effort)
    # The worker profile declares no routes, so every agy route keeps it.
    assert adapters.profile_for(agy, "worker", "gemini-3.8-flash-medium", "medium")


def test_build_candidates_admits_only_hydrogen_high_as_agy_reviewer(env):
    cfg = _config(ALLOW, "agy@1/gemini-4-hydrogen@medium", "agy@1/claude-opus-5-5@low")
    built, skipped = candidates.build_candidates(None, "code_reviewer", probe=False,
                                                 allow_policies=[route_policy.user_policy(cfg)])
    agy_built = [c for c in built if c["harness"] == "agy"]
    assert [(c["model_id"], c["effort"]) for c in agy_built] == [("gemini-4-hydrogen", "high")]
    assert adapters.profile_for(_agy(), "reviewer", agy_built[0]["invocation_model_id"], "high")
    skips = _labels(skipped)
    # A user allow does not give a route a launch form: it is skipped as no-profile.
    assert skips["agy/claude-opus-5-5@low"]["category"] == "no-profile"
    # Dispatchable agy rows (no allow needed) are no reviewers either.
    assert skips["agy/claude-opus-5-5@medium"]["category"] == "no-profile"
    assert skips["agy/gemini-3.8-flash@medium"]["category"] == "no-profile"
    # Hydrogen@medium is no reviewer route at all.
    assert ("gemini-4-hydrogen", "medium") not in {(c["model_id"], c["effort"]) for c in built}


def test_integration_reviewer_never_gets_hydrogen(env):
    # H5 opened plan review (tests/test_h5_hydrogen_plan_reviewer.py); integration review stays closed.
    cfg = {"routing": {"user_policy": {"role_rules": [
        {"role": "integration_reviewer", "route": ALLOW, "state": "allow"}]}}}
    with db.connect(env / "runs.db") as con:
        built, _ = candidates.build_candidates(con, "integration_reviewer", probe=False,
                                               allow_policies=[route_policy.user_policy(cfg)])
    assert not [c for c in built if hydrogen_trials.is_hydrogen(c)]


def test_route_role_dispatches_the_allowed_hydrogen_reviewer_after_a_probe(env):
    cfg = _config(ALLOW)
    agy = _agy()
    with db.connect(env / "runs.db") as con:
        built, _ = candidates.build_candidates(con, "code_reviewer", probe=False,
                                               allow_policies=[route_policy.user_policy(cfg)])
        cand = next(c for c in built if c["harness"] == "agy")
        fp = route_probe.fingerprint(cand, agy)
        con.execute("INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                    "invocation_model_id, effort, result, probed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (route_probe.key(cand, agy), fp["harness"], fp["harness_version"], fp["adapter_hash"],
                     fp["profile"], fp["invocation_model_id"], fp["effort"], "pass", now_iso()))
        con.commit()
        seeded = {**cfg, "roles": {"code_reviewer": {"preferred_seed": [
            {"model_id": "gemini-4-hydrogen", "harness": "agy", "effort": "high"}]}}}
        decision = candidates.route_role(con, seeded, {"gear": "S", "risk": {"size_class": "S"}},
                                         "code_reviewer", probe=False, observe=True)
    assert decision["selected"] == routing.candidate_id(cand), decision


def test_build_argv_renders_plan_mode_for_hydrogen_and_refuses_other_agy_models(tmp_path):
    agy = _agy()
    out = tmp_path / "dispatch" / "reply.md"
    argv, prof = adapters.build_argv(agy, "reviewer", model="gemini-4-hydrogen-high", effort="high",
                                     cwd=tmp_path, output=out, include_dirs=[tmp_path])
    assert argv[:7] == ["agy", "--mode", "plan", "--print-timeout", "30m", "--model", "gemini-4-hydrogen-high"]
    assert "--dangerously-skip-permissions" not in argv
    assert prof["verify_worktree_unchanged"] is True
    for model, effort in [("gemini-3.8-flash-medium", "medium"), ("gemini-4-hydrogen-medium", "medium"),
                          ("Claude Opus 5.5 (Low)", "low")]:
        with pytest.raises(adapters.AdapterError, match="launches only"):
            adapters.build_argv(agy, "reviewer", model=model, effort=effort, cwd=tmp_path, output=out)


def test_manual_review_bypass_is_refused_without_the_exact_allow():
    c = {"model_id": hydrogen_trials.MODEL, "harness": "agy", "effort": "high",
         "invocation_model_id": "gemini-4-hydrogen-high"}
    assert "exact allow" in hydrogen_trials.route_refusal(c, role="code_reviewer", size=None, manual=True)
    assert "exact allow" in hydrogen_trials.manual_conformance(None, c, role="code_reviewer", size=None)
    # With the allow, only the probe evidence is left to check.
    assert "runs.db" in hydrogen_trials.manual_conformance(None, c, role="code_reviewer", size=None,
                                                           user_allowed=True)


def test_a_pinned_hydrogen_reviewer_needs_only_the_run_not_a_task(env):
    """dispatch --review-as, review --as and rerun check the pin with run_id and no scope (#1178)."""
    from office.state import Refused
    db.connect().close()
    # Past the scope gate: the remaining refusals are the exact allow, then the probe receipt.
    with pytest.raises(Refused, match="exact allow"):
        candidates.declared_decision(HYDROGEN, flag="--review-as", role="code_reviewer", config={}, run_id="r1")
    with pytest.raises(Refused, match="conformance receipt"):
        candidates.declared_decision(HYDROGEN, flag="--review-as", role="code_reviewer", config=_config(ALLOW),
                                     run_id="r1")
    # An executor still needs the task that classifies its size.
    with pytest.raises(Refused, match="classified run/task"):
        candidates.declared_decision("agy/gemini-4-hydrogen@medium", role="executor", config={}, run_id="r1")
    with pytest.raises(Refused, match="classified run/task"):
        candidates.declared_decision(HYDROGEN, flag="--review-as", role="code_reviewer", config=_config(ALLOW))


def test_route_policy_reports_no_profile_for_non_executor_roles():
    row = {"invocation_harness": "agy", "model_id": "gemini-4-hydrogen", "effort": "high"}
    cand = {"harness": "agy", "model_id": "gemini-4-hydrogen", "effort": "high"}
    state = {"state": "allow", "source": "user", "rule": ALLOW}
    pass_probe = {"result": "pass"}
    ok, why = routecmd._dispatch_view(row, state, True, False, candidate=cand, role="code_reviewer",
                                      probe=pass_probe)
    assert ok is False and "no launch profile" in why
    ok, why = routecmd._dispatch_view(row, state, False, True, candidate=cand, role="code_reviewer",
                                      probe=pass_probe)
    assert ok is False and "not installed" in why
    ok, why = routecmd._dispatch_view(row, state, True, True, candidate=cand, role="code_reviewer",
                                      probe=pass_probe)
    assert ok is True and why.startswith("user allow; exact probe passed; a voting code reviewer")
    assert "dispatches (" not in why


def test_harness_validate_rejects_a_malformed_routes_scope():
    from office import harnesscmd
    agy = _agy()
    bad = {**agy, "office_profiles": {**agy["office_profiles"],
                                      "reviewer": {**agy["office_profiles"]["reviewer"], "routes": ["no-effort"]}}}
    assert any("routes must be" in p for p in harnesscmd.check(bad)), harnesscmd.check(bad)
    assert not [p for p in harnesscmd.check(agy) if "reviewer" in p]


def test_pane_form_renders_plan_mode_with_the_survey_off_and_one_submit_allow(tmp_path):
    import json
    out = tmp_path / "dispatch" / "reply.txt"
    args, kind = adapters.interactive_argv(_agy(), "reviewer", model="gemini-4-hydrogen-high", effort="high",
                                           cwd=tmp_path, output=out, include_dirs=[tmp_path])
    assert kind == "agy" and args[:2] == ["--mode", "plan"]
    assert "--dangerously-skip-permissions" not in args and "accept-edits" not in args and "auto" not in args
    assert args[args.index("--model") + 1] == "gemini-4-hydrogen-high"
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings == {"showFeedbackSurvey": False, "permissions": {"allow": ["command(office review submit)"]}}
    # Without a reply to hand back there is no allow, only the survey switch.
    bare, _ = adapters.interactive_argv(_agy(), "reviewer", model="gemini-4-hydrogen-high", effort="high", cwd=tmp_path)
    assert json.loads(bare[bare.index("--settings") + 1]) == {"showFeedbackSurvey": False}
    with pytest.raises(adapters.AdapterError, match="launches only"):
        adapters.interactive_argv(_agy(), "reviewer", model="gemini-4-hydrogen-medium", effort="medium",
                                  cwd=tmp_path, output=out)


def test_the_agy_pane_pointer_asks_for_stdin_submit_not_a_file_write(tmp_path):
    from office import dispatch
    out = tmp_path / "reply.txt"
    hint = dispatch._handback_hint("agy", "reviewer", out)
    assert "office review submit" in hint and "standard input" in hint and "review-submit.txt" not in hint
    assert "review-submit.txt" in dispatch._handback_hint("claude", "reviewer", out)
    assert "review-submit.txt" in dispatch._handback_hint("agy", "vision", out)  # the vision pane may write
    assert dispatch._handback_hint("agy", "reviewer", None) == ""


def test_the_worktree_guard_stays_on_for_the_pane_reviewer(tmp_path):
    import subprocess
    from office import gates
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True)
    guard = gates._worktree_guard({"harness": "agy", "launcher": "herdr"}, "reviewer", repo)
    assert guard is not None
    before = guard(None)
    assert guard(before) is None
    (repo / "edited-by-the-pane-reviewer.txt").write_text("x")
    assert "untracked files" in guard(before)
