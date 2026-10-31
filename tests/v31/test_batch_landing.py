"""Issue #943: ordered native batch landing, single bump and final-suite retry."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from office import batch_landing as batch
from office import cli
from office.state import Refused, Usage


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Batch Test")
    git(repo, "config", "user.email", "batch@example.invalid")
    version = repo / "src" / "office"
    version.mkdir(parents=True)
    (version / "_version.py").write_text('__version__ = "3.14.0"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 3.14.0\n\n- initial\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-u", "origin", "main")
    return repo


def pr_branch(repo, number, changes):
    git(repo, "switch", "-c", f"pr{number}", "main")
    for name, text in changes.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "add", ".")
    git(repo, "commit", "-m", f"PR {number}")
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    return {"head": sha, "files": [{"path": x} for x in changes], "state": "OPEN",
            "base": "main", "draft": False}


def manifest_file(repo, lanes, **kwargs):
    data = {"id": "release-a", "base": "main", "version": "3.15.0",
            "full_suite": ["python3", "-c", "from pathlib import Path; assert Path('a.txt').is_file()"],
            "lanes": lanes, **kwargs}
    p = repo.parent / "_batch.json"
    p.write_text(json.dumps(data))
    return p


def owner_files():
    return {"src/office/_version.py": '__version__ = "3.15.0"\n',
            "CHANGELOG.md": "# Changelog\n\n## 3.15.0\n\n- batch\n\n## 3.14.0\n\n- initial\n"}


class Hub:
    def __init__(self, repo, prs, events):
        self.repo, self.prs, self.events = repo, prs, events

    def __call__(self, repo, *argv, **kwargs):
        assert repo == self.repo
        assert argv[0] == "pr"
        number = int(argv[2])
        pr = self.prs[number]
        if argv[1] == "view":
            info = {"number": number, "state": pr["state"], "baseRefName": pr["base"],
                    "headRefOid": pr["head"], "isDraft": pr["draft"], "files": pr["files"]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")
        assert argv[1] == "merge"
        self.events.append(f"merge:{number}")
        assert "--match-head-commit" in argv
        assert argv[-1] == pr["head"]
        git(repo, "switch", "main")
        git(repo, "fetch", "origin", "main")
        git(repo, "reset", "--hard", "origin/main")
        git(repo, "merge", "--no-ff", "--no-edit", f"pr{number}")
        git(repo, "push", "origin", "main")
        pr["state"] = "MERGED"
        return subprocess.CompletedProcess(argv, 0, "merged", "")


def setup_two(tmp_path, monkeypatch):
    repo = fixture_repo(tmp_path)
    prs = {
        1: pr_branch(repo, 1, {"a.txt": "core\n"}),
        2: pr_branch(repo, 2, owner_files()),
    }
    events = []
    monkeypatch.setattr(batch, "_github", Hub(repo, prs, events))
    monkeypatch.setattr(batch.land, "_wait_checks",
                        lambda run, number: events.append(f"checks:{number}"))
    m = manifest_file(repo, [
        {"id": "A", "pr": 1},
        {"id": "B", "pr": 2, "land_after": ["A"], "version_bump": True},
    ])
    return repo, prs, events, m


def test_manifest_orders_dependencies_even_when_out_of_order(tmp_path):
    p = tmp_path / "batch.json"
    p.write_text(json.dumps({"id": "release", "version": "3.15.0",
                             "full_suite": ["true"],
                             "lanes": [{"id": "C", "pr": 3, "land_after": ["B"]},
                                       {"id": "A", "pr": 1, "version_bump": True},
                                       {"id": "B", "pr": 2, "land_after": ["A"]}]}))
    assert [e["id"] for e in batch._manifest(p)["lanes"]] == ["A", "B", "C"]
    obj = json.loads(p.read_text())
    obj["lanes"][1]["land_after"] = ["C"]
    p.write_text(json.dumps(obj))
    with pytest.raises(Usage, match="cycle"):
        batch._manifest(p)


@pytest.mark.parametrize("change", [
    lambda m: m["lanes"][0].update(version_bump=True),
    lambda m: m["lanes"][1].update(land_after=["missing"]),
    lambda m: m["lanes"][1].update(id="A"),
    lambda m: m.update(full_suite=["true", "&&", "false"]),
])
def test_manifest_refuses_unsafe_plans(tmp_path, change):
    obj = {"id": "release", "version": "3.15.0", "full_suite": ["true"],
           "lanes": [{"id": "A", "pr": 1}, {"id": "B", "pr": 2, "version_bump": True}]}
    change(obj)
    p = tmp_path / "batch.json"
    p.write_text(json.dumps(obj))
    with pytest.raises(Usage):
        batch._manifest(p)


def test_dry_run_only_preflights_and_leaves_main_untouched(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    before = git(repo, "rev-parse", "origin/main")
    result = batch.land_batch(str(m), cwd=repo, dry_run=True)
    assert result.data["ordered_lanes"] == ["A", "B"]
    assert git(repo, "rev-parse", "origin/main") == before
    assert not batch._state_path(repo, "release-a").exists()
    assert events == []


def test_merge_order_single_version_owner_and_one_final_suite(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    real_suite = batch._run_full

    def suite(*args):
        events.append("suite")
        return real_suite(*args)

    monkeypatch.setattr(batch, "_run_full", suite)
    result = batch.land_batch(str(m), cwd=repo)
    assert events == ["checks:1", "merge:1", "checks:2", "merge:2", "suite"]
    assert result.data["batch"]["status"] == "green"
    assert result.data["batch"]["validated"]["sha"] == git(repo, "rev-parse", "origin/main")
    assert git(repo, "show", "origin/main:src/office/_version.py").strip() == '__version__ = "3.15.0"'
    assert "## 3.15.0" in git(repo, "show", "origin/main:CHANGELOG.md")
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"
    assert events.count("suite") == 1


def test_first_failed_lane_stops_before_next_merge_and_suite(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    before = git(repo, "rev-parse", "origin/main")

    def checks(run, number):
        events.append(f"checks:{number}")
        if number == 1:
            raise Refused("checks-failed", "no green checks")

    monkeypatch.setattr(batch.land, "_wait_checks", checks)
    monkeypatch.setattr(batch, "_run_full", lambda *a: pytest.fail("suite ran before merges"))
    with pytest.raises(Refused, match="A:"):
        batch.land_batch(str(m), cwd=repo)
    assert events == ["checks:1"]
    assert git(repo, "rev-parse", "origin/main") == before
    status = json.loads(batch._state_path(repo, "release-a").read_text())
    assert status["status"] == "blocked" and status["failed_lane"]["id"] == "A"


def test_double_version_owner_refused_before_first_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[1]["files"].append({"path": "src/office/_version.py"})
    before = git(repo, "rev-parse", "origin/main")
    with pytest.raises(Refused, match="exactly one PR"):
        batch.land_batch(str(m), cwd=repo)
    assert git(repo, "rev-parse", "origin/main") == before
    assert events == []


def test_red_batch_requires_fix_lane_then_rechecks_new_final_sha(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    pr_branch(repo, 3, {"fix.txt": "good\n"})
    prs[3] = {"head": git(repo, "rev-parse", "pr3"), "files": [{"path": "fix.txt"}],
              "state": "OPEN", "base": "main", "draft": False}
    suite_calls = []

    def suite(repo, sha, manifest, folder):
        suite_calls.append(sha)
        path = folder / "full-fake.log"
        path.write_text("red\n" if len(suite_calls) == 1 else "green\n")
        events.append("suite")
        return (1 if len(suite_calls) == 1 else 0), path

    monkeypatch.setattr(batch, "_run_full", suite)
    with pytest.raises(Refused, match="full suite exited 1"):
        batch.land_batch(str(m), cwd=repo)
    assert len(suite_calls) == 1
    with pytest.raises(Refused, match="append an ordered fix"):
        batch.land_batch(str(m), cwd=repo)
    assert len(suite_calls) == 1
    data = json.loads(m.read_text())
    data["lanes"].append({"id": "fix", "pr": 3, "land_after": ["B"]})
    m.write_text(json.dumps(data))
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"
    assert events[-2:] == ["merge:3", "suite"]
    assert len(suite_calls) == 2 and suite_calls[0] != suite_calls[1]


def test_resuming_moved_main_refuses_unrecorded_writes(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    monkeypatch.setattr(batch, "_run_full",
                        lambda repo, sha, manifest, folder: (1, folder / "red.log"))
    with pytest.raises(Refused, match="full suite exited"):
        batch.land_batch(str(m), cwd=repo)
    git(repo, "switch", "main")
    git(repo, "fetch", "origin", "main")
    (repo / "out-of-band.txt").write_text("external\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "external main change")
    git(repo, "push", "origin", "main")
    with pytest.raises(Refused, match="moved outside this batch"):
        batch.land_batch(str(m), cwd=repo, retry_full=True)



def test_wrong_version_owner_refuses_before_any_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    git(repo, "switch", "pr2")
    (repo / "src" / "office" / "_version.py").write_text('__version__ = "3.99.0"\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "wrong bump")
    prs[2]["head"] = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    before = git(repo, "rev-parse", "origin/main")
    with pytest.raises(Refused, match="version"):
        batch.land_batch(str(m), cwd=repo)
    assert git(repo, "rev-parse", "origin/main") == before
    assert events == []


def test_run_lane_accepts_stacked_prs_with_parent_base(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[2]["base"] = "pr1"
    run = {"id": "run-123", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo, base: (run, [1, 2]))
    data = batch._manifest(m)
    data["lanes"] = [{"id": "run-lane", "run": "run-123", "version_bump": True}]
    entries, infos = batch._entries(data, object(), repo)
    assert entries[0]["prs"] == [1, 2]
    assert infos[2]["baseRefName"] == "pr1"


def test_unaccepted_run_rejected_before_merges(tmp_path, monkeypatch):
    repo = fixture_repo(tmp_path)
    run = {"id": "bad-run", "repo_root": str(repo)}
    monkeypatch.setattr(batch.state, "find_run", lambda con, ident: run)
    monkeypatch.setattr(batch.land, "end_state", lambda con, run: {"mode": "merge"})
    monkeypatch.setattr(batch.land, "_snapshot",
                        lambda con, run: {"commit": None, "accepted": None})
    with pytest.raises(Refused, match="integration is not accepted"):
        batch._run_lane(object(), {"id": "unaccepted", "run": "bad-run"}, repo, "main")


def test_green_batch_rejects_changed_main_without_revalidation(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    batch.land_batch(str(m), cwd=repo)
    git(repo, "switch", "main")
    git(repo, "fetch", "origin", "main")
    git(repo, "reset", "--hard", "origin/main")
    (repo / "external.txt").write_text("external\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "external")
    git(repo, "push", "origin", "main")
    with pytest.raises(Refused, match="moved after the green batch"):
        batch.land_batch(str(m), cwd=repo)

def test_cli_routing_without_active_run(tmp_path, monkeypatch):
    observed = {}

    def routed(manifest, *, cwd, dry_run, retry_full):
        observed.update(manifest=manifest, dry_run=dry_run, retry_full=retry_full)
        from office.result import Result
        return Result(lines=["batch planned"])

    monkeypatch.setattr(batch, "land_batch", routed)
    monkeypatch.setattr("office.route_probe.sweep", lambda: None)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["land", "--batch", "release.json", "--dry-run"]) == 0
    assert observed == {"manifest": "release.json", "dry_run": True, "retry_full": False}
    assert cli.main(["land", "--batch", "release.json", "--merge"]) == 2
