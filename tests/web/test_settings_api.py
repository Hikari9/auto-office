"""GET /api/settings: tiers, sources and apply semantics."""
from __future__ import annotations

import json

import pytest
import yaml

from office import db
from office.web import server, settings, synthetic


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    (tmp_path / "user.yaml").write_text(yaml.safe_dump({"scheduler": {"max_active_runs": 4, "aging_per_hour": 1.0}}))
    s = server.build_fixture("small", home=tmp_path / "fx").start()
    yield s
    s.close()


def by_key(view):
    return {e["key"]: e for e in view["entries"]}


def test_machine_scope_reports_sources_and_overrides(svc):
    view = svc.settings_view()
    assert view["scope"] == "machine" and view["tiers"] == ["default", "machine", "repository", "run-pinned"]
    e = by_key(view)
    cap = e["scheduler.max_active_runs"]
    assert cap["value"] == 4 and cap["source"] == "machine" and cap["overridden"] and not cap["inherited"]
    assert cap["values"]["default"] is None and cap["values"]["machine"] == 4
    same = e["scheduler.aging_per_hour"]
    assert same["source"] == "machine" and not same["overridden"]
    auto = e["scheduler.auto_mode"]
    assert auto["source"] == "default" and auto["inherited"] and auto["editable"] == ["machine"]
    assert not any(k.startswith(("hard_invariants", "schema_version")) for k in e)


def test_repository_scope_layers_the_repo_file(svc):
    checkout = svc.checkouts("synth-org-0/repo-00")
    (checkout / ".auto-office").mkdir()
    (checkout / ".auto-office" / "config.yaml").write_text(yaml.safe_dump({"scheduler": {"max_active_runs": 2}}))
    e = by_key(svc.settings_view(repo="synth-org-0/repo-00"))
    cap = e["scheduler.max_active_runs"]
    assert cap["source"] == "repository" and cap["value"] == 2 and cap["set_in"] == ["default", "machine", "repository"]
    assert cap["editable"] == ["machine", "repository"]


def test_run_scope_shows_the_pinned_policy(svc):
    con = db.connect(svc.db_path)
    with db.transaction(con):
        synthetic.insert_run(con, "PINNED", git_common_dir="/nowhere/.git")
        con.execute("UPDATE runs SET policy_json=? WHERE id='PINNED'",
                    (json.dumps({"scheduler": {"max_active_runs": 9}, "hard_invariants": ["x"]}),))
    con.close()
    view = svc.settings_view(run_id="PINNED")
    assert view["scope"] == "run" and view["run"] == "run:PINNED"
    cap = by_key(view)["scheduler.max_active_runs"]
    assert cap["source"] == "run-pinned" and cap["value"] == 9 and cap["values"]["machine"] == 4


@pytest.mark.parametrize("key,when", [("scheduler.max_active_runs", "immediate"), ("quota.reserve_percent",
                                      "before-dispatch"), ("roles.executor.x", "before-dispatch"),
                                      ("paths.runs_db", "restart"), ("paths.repo", "future-runs"),
                                      ("review.max_rounds", "future-runs")])
def test_apply_semantics(key, when):
    assert settings.apply_of(key) == when


def test_config_args():
    assert settings.config_args("settings_set", "machine", "a.b", True) == ["config", "--user", "--", "a.b", "true"]
    assert settings.config_args("settings_set", "repository", "a.b", "x") == ["config", "--repo", "--", "a.b", "x"]
    # A value that looks like a flag stays a value: it cannot switch tier, unset or force.
    assert settings.config_args("settings_set", "machine", "a.b", "--repo")[-3:] == ["--", "a.b", "--repo"]
    assert settings.config_args("settings_unset", "machine", "a.b") == ["config", "--user", "--unset", "--", "a.b"]
    from office import cli
    args = cli._parser().parse_args(settings.config_args("settings_set", "machine", "a.b", "--repo"))
    assert (args.tier, args.key, args.value, args.unset) == ("user", "a.b", "--repo", False)
    args = cli._parser().parse_args(settings.config_args("settings_unset", "repository", "a.b"))
    assert (args.tier, args.key, args.value, args.unset) == ("repo", "a.b", None, True)
    with pytest.raises(ValueError):
        settings.config_args("settings_set", "run-pinned", "a.b", 1)
