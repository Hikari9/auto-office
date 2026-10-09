"""First-run /auto-office onboarding (#484): `office onboard` and current-harness integration.

Unit tests run in an isolated home with fake harness binaries that only print a
version, so the shipped trust baseline decides trust as it does on a fresh install.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from office import cli, configcmd, discovery, onboarding, version
from office.state import OfficeError

ALL_SIGNED_IN = {"claude": "found", "codex": "found", "agy": "found", "gemini": "found"}
VERSIONS = {"claude": "2.1.300", "codex": "0.160.0", "agy": "1.3.2"}


class Home:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.user = tmp / "user-config.yaml"
        self.repo = tmp / "repo"
        self.repo_config = self.repo / ".auto-office" / "config.yaml"
        self.claude_settings = self.home / ".claude" / "settings.json"
        self.gemini_settings = self.home / ".gemini" / "settings.json"

    def office(self, *args) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(list(args))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return code, out.getvalue()

    def ojson(self, *args) -> tuple[int, dict]:
        code, out = self.office(*args, "--json")
        return code, json.loads(out[out.index("{"):])

    def status(self, harness: str = "claude") -> dict:
        code, data = self.ojson("onboard", "--harness", harness)
        assert code == 0, data
        return data["data"]

    def question(self, qid: str, harness: str = "claude") -> dict:
        return next(q for q in self.status(harness)["questions"] if q["id"] == qid)

    def user_data(self) -> dict:
        return yaml.safe_load(self.user.read_text()) if self.user.exists() else {}


@pytest.fixture
def h(tmp_path, monkeypatch):
    t = Home(tmp_path)
    t.home.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, ver in VERSIONS.items():
        exe = bin_ / name
        exe.write_text(f"#!/bin/sh\necho {ver}\n")
        exe.chmod(0o755)
    for k in list(os.environ):
        if k.startswith(("OFFICE_", "HERDR_", "AUTO_OFFICE_")) or k in (
                "CLAUDECODE", "GEMINI_CLI", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(t.home))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(t.user))
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
    monkeypatch.setenv("OFFICE_AUTH_FIXTURE", json.dumps(ALL_SIGNED_IN))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_), "/usr/bin", "/bin"]))
    monkeypatch.setattr(discovery, "process_key", lambda: None)
    t.repo.mkdir()
    subprocess.run(["git", "init", "-q", str(t.repo)], check=True)
    monkeypatch.chdir(t.repo)
    return t


# ------------------------------------------------------------------ first run

def test_first_run_is_due_and_offers_office_decide_seeded_routes_and_custom(h):
    data = h.status()
    assert data["due"] is True and data["completed_schema"] == 0 and data["schema_version"] == onboarding.SCHEMA_VERSION
    assert [q["id"] for q in data["questions"]] == ["planner", "executor", "reviewer"]
    assert data["harness"]["current"] == "claude" and data["harness"]["source"] == "--harness"
    assert data["disclaimer"] == onboarding.DISCLAIMER
    for q in data["questions"]:
        assert q["options"][0] == {**q["options"][0], "answer": "office", "label": "Let Office decide", "recommended": True}
        assert q["options"][-1]["answer"] == "custom"
        assert q["current"]["choice"] == "office" and q["eligible"]
        for o in q["options"][1:-1]:
            assert o["answer"] in q["eligible"]
    planner = data["questions"][0]
    # The shipped planner seed (opus@medium, astra@low), each as its first eligible route, in seed order.
    assert [o["answer"] for o in planner["options"] if o.get("seeded")] == ["claude/opus@medium", "codex/astra@low"]
    assert "orchestrator" not in json.dumps([q["roles"] for q in data["questions"]])


def test_reviewer_and_executor_routes_follow_derived_trust(h):
    reviewer = h.question("reviewer")
    # code_reviewer needs proven trust: on a fresh install only the shipped baseline is proven.
    assert "codex/gpt-6-luna@high" in reviewer["eligible"]
    assert "claude/opus@low" not in reviewer["eligible"]
    why = {u["route"]: u["reason"] for u in reviewer["unavailable"]}
    assert "not proven" in why["claude/opus@low"] and "office approve trust" in why["claude/opus@low"]
    executor = h.question("executor")
    assert "agy/gemini-3.8-flash@medium" in executor["eligible"]
    assert not [o for o in executor["options"] if o.get("seeded")]  # executor ships no seed: adaptive routing decides
    # agy has no reviewer launch profile, so it is never a reviewer choice.
    assert not [r for r in reviewer["eligible"] if r.startswith("agy/")]


def test_status_lists_installed_harnesses_with_sign_in_and_never_writes(h):
    data = h.status()
    rows = {r["harness"]: r for r in data["harnesses"]}
    assert rows["claude"]["installed"] and rows["claude"]["version"] == "2.1.300" and rows["claude"]["auth"] == "found"
    assert rows["gemini"]["installed"] is False
    assert not h.user.exists() and not h.claude_settings.exists()


def test_current_harness_detection_order(h, monkeypatch):
    assert onboarding.current_harness("codex") == ("codex", "--harness")
    monkeypatch.setenv("CLAUDECODE", "1")
    assert onboarding.current_harness() == ("claude", "CLAUDECODE")
    monkeypatch.setenv("OFFICE_HARNESS", "agy")
    assert onboarding.current_harness() == ("agy", "OFFICE_HARNESS")
    monkeypatch.delenv("OFFICE_HARNESS")
    monkeypatch.delenv("CLAUDECODE")
    monkeypatch.setattr(discovery, "process_key", lambda: "codex:123:Mon")
    assert onboarding.current_harness() == ("codex", "process tree")
    monkeypatch.setattr(discovery, "process_key", lambda: None)
    assert onboarding.current_harness() == (None, "unknown")
    code, data = h.ojson("onboard")
    assert code == 0 and data["data"]["harness"]["integration"]["state"] == "unknown"
    assert data["data"]["harness"]["offer"] == []


# ------------------------------------------------------------------ apply

def test_apply_writes_role_seeds_and_marker_in_one_write(h):
    code, out = h.office("onboard", "--planner", "codex/astra@low", "--executor", "agy/gemini-3.8-flash@medium",
                         "--reviewer", "codex/gpt-6-luna@high")
    assert code == 0 and "onboarding complete" in out and onboarding.DISCLAIMER in out, out
    data = h.user_data()
    roles = data["roles"]
    assert roles["planner"]["preferred_seed"] == [{"model_id": "astra", "harness": "codex", "effort": "low"}]
    assert roles["executor"]["preferred_seed"] == [{"model_id": "gemini-3.8-flash", "harness": "agy", "effort": "medium"}]
    luna = [{"model_id": "gpt-6-luna", "harness": "codex", "effort": "high"}]
    assert roles["plan_reviewer"]["preferred_seed"] == luna and roles["code_reviewer"]["preferred_seed"] == luna
    assert set(roles) == {"planner", "executor", "plan_reviewer", "code_reviewer"}  # no orchestrator, no visual reviewer
    assert data["onboarding"] == {"schema_version": onboarding.SCHEMA_VERSION}
    assert "&id" not in h.user.read_text()  # plain YAML a person can edit
    status = h.status()
    assert status["due"] is False
    reviewer = next(q for q in status["questions"] if q["id"] == "reviewer")
    assert reviewer["current"] == {**reviewer["current"], "choice": "route", "value": "codex/gpt-6-luna@high",
                                   "eligible": True}


def test_reapplying_the_same_answers_is_idempotent(h):
    args = ("onboard", "--planner", "claude/opus@medium", "--executor", "office", "--reviewer", "keep")
    h.office(*args)
    first = h.user.read_text()
    code, data = h.ojson(*args)
    assert code == 0 and data["data"]["changed"] == [] and h.user.read_text() == first


def test_let_office_decide_clears_only_that_user_preference(h):
    h.user.write_text(yaml.safe_dump({
        "cost_policy": {"default": "quota_saver"},
        "roles": {"planner": {"preferred_seed": [{"model_id": "astra", "harness": "codex", "effort": "low"}]},
                  "code_reviewer": {"preferred_seed": [{"model_id": "gpt-6-luna", "harness": "codex", "effort": "xhigh"}]},
                  "visual_reviewer": {"preferred_seed": [{"model_id": "sonnet", "harness": "claude", "effort": "high"}]}}}))
    code, out = h.office("onboard", "--reviewer", "office")
    assert code == 0, out
    data = h.user_data()
    assert "code_reviewer" not in data["roles"] and "plan_reviewer" not in data["roles"]
    assert data["roles"]["planner"]["preferred_seed"][0]["model_id"] == "astra"  # untouched: --planner defaulted to keep
    assert data["roles"]["visual_reviewer"]["preferred_seed"][0]["model_id"] == "sonnet"
    assert data["cost_policy"] == {"default": "quota_saver"}


# ------------------------------------------------------------------ existing users

def test_existing_preferences_are_prefilled_with_repo_override_shown(h):
    h.user.write_text(yaml.safe_dump({"roles": {
        "code_reviewer": {"preferred_seed": [{"model_id": "gpt-6-luna", "harness": "codex", "effort": "high"}]},
        "executor": {"preferred_seed": [{"model_id": "sonnet", "harness": "claude", "effort": "high"},
                                        {"model_id": "gpt-6-luna", "harness": "codex", "effort": "high"}]}}}))
    h.repo_config.parent.mkdir(parents=True)
    h.repo_config.write_text(yaml.safe_dump({"roles": {"planner": {"preferred_seed": [{"model_id": "opus", "effort": "high"}]}}}))
    qs = {q["id"]: q for q in h.status()["questions"]}
    assert qs["planner"]["current"]["choice"] == "office"
    assert qs["planner"]["current"]["repo_override"] == {"planner": "opus@high"}
    assert qs["executor"]["current"]["value"] == "claude/sonnet@high,codex/gpt-6-luna@high"
    assert qs["executor"]["options"][1] == {**qs["executor"]["options"][1], "answer": "keep",
                                            "label": "Keep claude/sonnet@high,codex/gpt-6-luna@high (current)"}
    assert qs["reviewer"]["options"][1]["answer"] == "keep"
    assert qs["reviewer"]["current"]["choice"] == "mixed"
    assert qs["reviewer"]["current"]["value"] == {"plan_reviewer": "office", "code_reviewer": "codex/gpt-6-luna@high"}
    repo_before = h.repo_config.read_bytes()
    code, out = h.office("onboard", "--planner", "codex/astra@low")
    assert code == 0 and "still overrides planner (opus@high)" in out, out
    assert h.repo_config.read_bytes() == repo_before
    assert h.user_data()["roles"]["executor"]["preferred_seed"][1]["model_id"] == "gpt-6-luna"  # kept as it was
    # The repo file still wins for routing in this repository.
    assert configcmd.role_preferences(["planner"])["planner"]["origin"] == "repo"


# ------------------------------------------------------------------ skip

def test_skip_marks_complete_and_keeps_every_preference(h):
    prefs = {"cost_policy": {"default": "money_saver"},
             "roles": {"executor": {"preferred_seed": [{"model_id": "sonnet", "harness": "claude", "effort": "high"}]}}}
    h.user.write_text(yaml.safe_dump(prefs))
    h.repo_config.parent.mkdir(parents=True)
    h.repo_config.write_text("roles:\n  planner:\n    preferred_seed: [{model_id: astra}]\n")
    (h.home / ".claude").mkdir()
    h.claude_settings.write_text('{"theme": "dark"}\n')
    repo_before, settings_before = h.repo_config.read_bytes(), h.claude_settings.read_bytes()
    code, out = h.office("onboard", "--skip")
    assert code == 0 and "preferences unchanged" in out, out
    assert h.user_data() == {**prefs, "onboarding": {"schema_version": onboarding.SCHEMA_VERSION}}
    assert h.repo_config.read_bytes() == repo_before and h.claude_settings.read_bytes() == settings_before
    assert h.status()["due"] is False
    # Skipping again writes nothing.
    text = h.user.read_text()
    h.office("onboard", "--skip")
    assert h.user.read_text() == text


def test_skip_takes_no_answers_and_apply_needs_one(h):
    code, out = h.office("onboard", "--skip", "--planner", "office")
    assert code == 2 and "takes no answers" in out
    with pytest.raises(OfficeError):
        onboarding.apply({"planner": None})
    assert not h.user.exists()


# ------------------------------------------------------------------ versioning

def test_schema_bump_reprompts_with_current_values_but_a_package_bump_does_not(h, monkeypatch):
    h.office("onboard", "--executor", "agy/gemini-3.8-flash@medium")
    assert h.status()["due"] is False
    monkeypatch.setattr(version, "current", lambda: "9.9.9")
    assert h.status()["due"] is False  # a new package version alone never re-onboards
    monkeypatch.setattr(onboarding, "SCHEMA_VERSION", onboarding.SCHEMA_VERSION + 1)
    data = h.status()
    assert data["due"] is True and "newer than the one completed" in data["reason"]
    executor = next(q for q in data["questions"] if q["id"] == "executor")
    assert executor["current"]["value"] == "agy/gemini-3.8-flash@medium"  # prefilled, not erased
    h.office("onboard", "--skip")
    assert h.user_data()["onboarding"]["schema_version"] == onboarding.SCHEMA_VERSION
    assert h.user_data()["roles"]["executor"]["preferred_seed"][0]["harness"] == "agy"


def test_onboarding_state_is_user_level_only(h):
    h.repo_config.parent.mkdir(parents=True)
    h.repo_config.write_text(f"onboarding:\n  schema_version: {onboarding.SCHEMA_VERSION}\n")
    assert h.status()["due"] is True  # a repo cannot mark a user onboarded
    code, out = h.office("config", "--repo", "onboarding.schema_version", "1")
    assert code == 2 and "user-level" in out
    code, out = h.office("config", "onboarding.schema_version", "0")
    assert code == 0, out  # the user may reset it to see onboarding again


# ------------------------------------------------------------------ eligibility

@pytest.mark.parametrize("answer,fragment", [
    ("claude/sonet@high", "did you mean sonnet"),          # unknown model
    ("claude/opus@none", "no none effort"),                # unsupported effort
    ("codex/opus@high", "runs on claude, not codex"),      # wrong harness
    ("opus@high", "harness/model@effort"),                 # not a full route
    ("claude/opus@low", "not proven"),                     # trust floor for code review
    ("agy/gemini-3.8-flash@medium", "no reviewer launch profile"),  # role-ineligible
])
def test_invalid_reviewer_answers_are_rejected_and_nothing_is_written(h, answer, fragment):
    code, out = h.office("onboard", "--planner", "claude/opus@medium", "--reviewer", answer)
    assert code == 2 and fragment in out and "eligible:" in out, out
    assert not h.user.exists() and h.status()["due"] is True


def test_signed_out_harness_is_not_offered_or_accepted(h, monkeypatch):
    monkeypatch.setenv("OFFICE_AUTH_FIXTURE", json.dumps({**ALL_SIGNED_IN, "codex": "missing"}))
    planner = h.question("planner")
    assert not [r for r in planner["eligible"] if r.startswith("codex/")]
    assert [o["answer"] for o in planner["options"] if o.get("seeded")] == ["claude/opus@medium"]
    code, out = h.office("onboard", "--planner", "codex/astra@low")
    assert code == 2 and "not signed in" in out, out


def test_uninstalled_harness_routes_are_unavailable(h):
    planner = h.question("planner")
    why = {u["route"]: u["reason"] for u in planner["unavailable"]}
    assert any("gemini not installed" in r or "no worker launch profile" in r for r in why.values())
    assert not [r for r in planner["eligible"] if r.startswith("gemini/")]


def test_credentials_checks_files_not_binaries(h, monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE")
    monkeypatch.setenv("CODEX_HOME", str(h.home / ".codex"))
    assert onboarding.credentials("codex")[0] == "missing"
    (h.home / ".codex").mkdir()
    (h.home / ".codex" / "auth.json").write_text("{}")
    assert onboarding.credentials("codex")[0] == "found"
    assert onboarding.credentials("agy")[0] == "missing"
    assert onboarding.credentials("hermes")[0] == "unknown"


def test_an_interrupted_write_leaves_onboarding_due(h, monkeypatch):
    h.user.write_text("cost_policy:\n  default: money_saver\n")

    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(configcmd, "_write", boom)
    with pytest.raises(OSError):
        onboarding.apply({"planner": "claude/opus@medium"})
    assert h.user.read_text() == "cost_policy:\n  default: money_saver\n"
    monkeypatch.undo()


# ------------------------------------------------------------------ routing inputs

def test_answers_reach_the_existing_routing_inputs_unchanged(h):
    from office import candidates, db
    from office import config as cfg
    h.office("onboard", "--planner", "claude/opus@medium", "--executor", "agy/gemini-3.8-flash@medium",
             "--reviewer", "codex/gpt-6-luna@high")
    config, _ = cfg.resolve(h.repo)
    assert config["routing"]["adaptive"]["weights"]["balanced"]["preference"] == 0.10  # weights untouched
    con = db.connect()
    try:
        run = {"id": None, "gear": "full"}
        planner = candidates.route_role(con, config, run, "planner", probe=False)
        assert planner["request"]["preferred_seed"] == [{"model_id": "opus", "harness": "claude", "effort": "medium"}]
        assert planner["selected"].endswith("/opus@medium")  # planner keeps its stronger advisory seed
        executor = candidates.route_role(con, config, run, "executor", probe=False)
        assert executor["request"]["preferred_seed"] == [
            {"model_id": "gemini-3.8-flash", "harness": "agy", "effort": "medium"}]
        reviewer = candidates.route_role(con, config, run, "code_reviewer", probe=False)
        assert reviewer["selected"].endswith("/gpt-6-luna@high")
        visual = candidates.route_role(con, config, run, "visual_reviewer", probe=False)
        assert visual["request"]["preferred_seed"][0]["model_id"] == "gemini-3.8-flash"  # its own shipped seed
    finally:
        con.close()


# ------------------------------------------------------------------ current-harness integration

def test_integration_missing_offers_three_actions_for_the_current_harness_only(h):
    data = h.status()
    assert data["harness"]["integration"]["state"] == "harness-missing" and data["harness"]["offer"] == []
    (h.home / ".claude").mkdir()
    (h.home / ".gemini").mkdir()
    data = h.status()
    integ = data["harness"]["integration"]
    assert integ["state"] == "missing"
    assert data["harness"]["offer"] == ["install-recommended", "review-individually", "skip"]
    assert data["commands"]["install_recommended"] == "office install --only claude"
    assert [i["id"] for i in integ["items"]] == ["session.start", "prompt.submit", "tool.pre", "read-rules"]
    assert all(i["state"] == "missing" and i["change"] for i in integ["items"])
    assert "SessionStart hook in" in integ["items"][0]["change"]
    assert not h.claude_settings.exists() and not h.gemini_settings.exists()  # status never writes


def test_review_individually_applies_only_the_accepted_items(h):
    (h.home / ".claude").mkdir()
    (h.home / ".gemini").mkdir()
    user_hook = {"hooks": [{"type": "command", "command": "echo mine"}]}
    h.claude_settings.write_text(json.dumps({"hooks": {"SessionStart": [user_hook]}, "permissions": {"allow": ["Bash(ls)"]}}))
    code, out = h.office("install", "--only", "claude", "--item", "session.start")
    assert code == 0 and "hooks installed (SessionStart)" in out, out
    data = json.loads(h.claude_settings.read_text())
    assert set(data["hooks"]) == {"SessionStart"} and user_hook in data["hooks"]["SessionStart"]
    assert data["permissions"]["allow"] == ["Bash(ls)"]  # read rules not accepted, not written
    assert not h.gemini_settings.exists()  # never another harness
    integ = h.status()["harness"]["integration"]
    assert integ["state"] == "partial"
    assert {i["id"]: i["state"] for i in integ["items"]} == {
        "session.start": "current", "prompt.submit": "missing", "tool.pre": "missing", "read-rules": "missing"}
    code, out = h.office("install", "--only", "claude", "--item", "read-rules")
    assert code == 0 and "settings updated" in out, out
    assert "Bash(ls)" in json.loads(h.claude_settings.read_text())["permissions"]["allow"]
    code, out = h.office("install", "--only", "claude", "--item", "bogus")
    assert code == 2 and "unknown install item" in out


def test_install_recommended_then_rerun_is_current_and_stale_is_detected(h):
    (h.home / ".claude").mkdir()
    (h.home / ".gemini").mkdir()
    code, out = h.office("install", "--only", "claude")
    assert code == 0 and "hooks installed" in out, out
    assert not h.gemini_settings.exists()
    data = h.status()
    assert data["harness"]["integration"]["state"] == "installed" and data["harness"]["offer"] == []
    before = h.claude_settings.read_text()
    code, out = h.office("install", "--only", "claude")
    assert "hooks already current" in out and h.claude_settings.read_text() == before
    settings = json.loads(before)
    settings["hooks"]["SessionStart"][-1]["hooks"][0]["command"] = "/old/office hook session.start --harness claude --office-managed"
    h.claude_settings.write_text(json.dumps(settings))
    integ = h.status()["harness"]["integration"]
    assert integ["state"] == "stale" and integ["items"][0]["state"] == "stale"
    assert h.status()["harness"]["offer"] == ["install-recommended", "review-individually", "skip"]
    # Uninstall still removes every Office-managed entry, item-installed or not.
    h.office("uninstall")
    assert not [e for e in json.loads(h.claude_settings.read_text())["hooks"]["SessionStart"]
                if "--office-managed" in json.dumps(e)]


@pytest.mark.parametrize("harness", ["codex", "agy", "hermes", "pi"])
def test_harnesses_without_verified_hooks_are_reported_not_offered(h, harness):
    data = h.status(harness)
    integ = data["harness"]["integration"]
    assert integ["state"] == "unsupported" and integ["reason"] and data["harness"]["offer"] == []


def test_install_no_hooks_registers_the_runtime_and_writes_no_harness_config(h):
    (h.home / ".claude").mkdir()
    code, out = h.office("install", "--no-hooks")
    assert code == 0 and "runtime" in out and "none written" in out, out
    assert not h.claude_settings.exists()


# ------------------------------------------------------------------ compatibility

def test_office_setup_still_writes_through_the_shared_path(h):
    answers = iter(["claude/opus@medium", "", "", "", "", ""])
    res = configcmd.setup(tier="user", yes=True, input_fn=lambda _: next(answers), out=lambda _: None, interactive=True)
    assert res.data["changed"] == ["roles.planner.preferred_seed"]
    assert h.user_data() == {"roles": {"planner": {"preferred_seed": [
        {"model_id": "opus", "harness": "claude", "effort": "medium"}]}}}
    assert h.status()["due"] is True  # advanced setup does not complete onboarding by itself


def test_status_is_noninteractive_and_returns_without_a_terminal(h):
    proc = subprocess.run([sys.executable, "-m", "office", "onboard", "--harness", "claude", "--json"],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, cwd=h.repo,
                          env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["data"]["due"] is True


def test_skill_gates_intake_on_office_onboard():
    root = Path(__file__).resolve().parents[2]
    text = (root / "SKILL.md").read_text()
    gate = text[text.index("## First-run onboarding"):text.index("## Start")]
    for needle in ("office onboard --harness", "--skip", "Let Office decide", "office install --only",
                   "Review individually", "Headless", "continue the original request"):
        assert needle in gate, needle
    assert text.index("## Install check") < text.index("## First-run onboarding") < text.index("## Start")


# ------------------------------------------------------------------ onboarding -> intake (integration)

def _onboarding_env(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    monkeypatch.setenv("OFFICE_AUTH_FIXTURE", json.dumps(ALL_SIGNED_IN))
    for k in ("CLAUDECODE", "GEMINI_CLI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(discovery, "process_key", lambda: None)


def test_onboarding_then_intake_starts_a_run_pinned_to_the_answers(env, monkeypatch):
    from office import state
    _onboarding_env(env, monkeypatch)
    (env.home / ".claude").mkdir()
    code, data = env.ojson("onboard", "--harness", "claude")
    assert code == 0 and data["data"]["due"] is True and data["data"]["harness"]["offer"], data
    env.office("install", "--only", "claude", check=0)  # "Install recommended"
    env.office("onboard", "--planner", "claude/opus@medium", "--executor", "office", "--reviewer", "keep", check=0)
    code, data = env.ojson("onboard", "--harness", "claude")
    assert data["data"]["due"] is False and data["data"]["harness"]["integration"]["state"] == "installed"
    assert not (env.home / ".gemini").exists()
    # The pending request continues: intake runs with no second invocation and pins the answers.
    code, out = env.office("start", "fixture goal", "--gear", "direct+review", "--planner", "inline")
    assert code == 0, out
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    assert run["policy"]["roles"]["planner"]["preferred_seed"] == [
        {"model_id": "opus", "harness": "claude", "effort": "medium"}]


def test_intake_is_not_blocked_while_onboarding_is_due_and_running_runs_keep_their_pin(env, monkeypatch):
    from office import state
    _onboarding_env(env, monkeypatch)
    code, out = env.office("start", "fixture goal", "--gear", "direct+review", "--planner", "inline")
    assert code == 0, out  # headless or not-yet-onboarded: intake proceeds on current defaults
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    pinned = state.get_run(con, run_id)["policy"]["roles"]["executor"].get("preferred_seed")
    env.office("onboard", "--executor", "agy/gemini-3.8-flash@medium", check=0)
    assert state.get_run(env.con(), run_id)["policy"]["roles"]["executor"].get("preferred_seed") == pinned
