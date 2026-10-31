"""Issue #943: ordered native batch landing, single bump, suite before any merge, and retry."""
from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from office import batch_landing as batch
from office import cli
from office.result import Result
from office.state import Refused, Usage


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
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
    """Fake GitHub. `extra` adds an unreviewed commit to main after each merge."""

    def __init__(self, repo, prs, events, extra=None):
        self.repo, self.prs, self.events, self.extra = repo, prs, events, extra
        self.merge_argv = []

    def __call__(self, repo, *argv, **kwargs):
        assert repo == self.repo
        assert argv[0] == "pr"
        number = int(argv[2])
        pr = self.prs[number]
        if argv[1] == "view":
            info = {"number": number, "state": pr["state"], "baseRefName": pr["base"],
                    "headRefName": f"pr{number}", "headRefOid": pr["head"], "isDraft": pr["draft"],
                    "files": pr["files"]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(info), "")
        if argv[1] == "edit":
            pr["base"] = argv[argv.index("--base") + 1]
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert argv[1] == "merge"
        self.merge_argv.append(list(argv))
        self.events.append(f"merge:{number}")
        assert "--match-head-commit" in argv
        assert argv[argv.index("--match-head-commit") + 1] == pr["head"]
        assert "--admin" not in argv
        git(repo, "switch", "main")
        git(repo, "fetch", "origin", "main")
        git(repo, "reset", "--hard", "origin/main")
        git(repo, "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit", f"pr{number}")
        if self.extra:
            (self.repo / self.extra).write_text(f"unreviewed after #{number}\n")
            git(repo, "add", ".")
            git(repo, "-c", "commit.gpgsign=false", "commit", "-m", f"unreviewed change on main after #{number}")
        git(repo, "push", "origin", "main")
        pr["state"] = "MERGED"
        return subprocess.CompletedProcess(argv, 0, "merged", "")


def setup_two(tmp_path, monkeypatch, owner=None):
    repo = fixture_repo(tmp_path)
    prs = {
        1: pr_branch(repo, 1, {"a.txt": "core\n"}),
        2: pr_branch(repo, 2, owner or owner_files()),
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


def test_dry_run_only_validates_and_leaves_main_untouched(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    before = git(repo, "rev-parse", "origin/main")
    monkeypatch.setattr(batch, "_run_full", lambda *a: pytest.fail("dry run must not run the suite"))
    result = batch.land_batch(str(m), cwd=repo, dry_run=True)
    assert result.data["ordered_lanes"] == ["A", "B"]
    assert any("composed SHA" in line for line in result.lines)
    assert git(repo, "rev-parse", "origin/main") == before
    assert not batch._state_path(repo, "release-a").exists()
    assert events == []


def test_suite_runs_on_composed_sha_before_any_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    base = git(repo, "rev-parse", "origin/main")
    real_suite = batch._run_full

    def suite(repo_, sha, manifest, folder):
        events.append("suite")
        assert git(repo, "rev-parse", "origin/main") == base  # nothing merged yet
        assert sha != base and sha not in (git(repo, "rev-parse", "pr1"), git(repo, "rev-parse", "pr2"))
        return real_suite(repo_, sha, manifest, folder)

    monkeypatch.setattr(batch, "_run_full", suite)
    result = batch.land_batch(str(m), cwd=repo)
    assert events == ["suite", "checks:1", "merge:1", "checks:2", "merge:2"]
    assert events.index("suite") < events.index("merge:1")
    batch_data = result.data["batch"]
    assert batch_data["status"] == "green"
    assert batch_data["validated"]["sha"] == batch_data["composed"]
    assert batch_data["landed_head"] == git(repo, "rev-parse", "origin/main")
    assert git(repo, "rev-parse", "origin/main^{tree}") == git(repo, "rev-parse", f"{batch_data['composed']}^{{tree}}")
    assert git(repo, "show", "origin/main:src/office/_version.py").strip() == '__version__ = "3.15.0"'
    assert "## 3.15.0" in git(repo, "show", "origin/main:CHANGELOG.md")
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"
    assert events.count("suite") == 1


def test_every_pr_merge_pins_the_validated_head_and_never_uses_admin(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    hub = Hub(repo, prs, events)
    monkeypatch.setattr(batch, "_github", hub)
    batch.land_batch(str(m), cwd=repo)
    assert [argv[2] for argv in hub.merge_argv] == ["1", "2"]
    for argv in hub.merge_argv:
        assert "--admin" not in argv
        assert argv[argv.index("--match-head-commit") + 1] == prs[int(argv[2])]["head"]


def test_stacked_run_pr_is_retargeted_then_merged_on_its_pinned_head(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[2]["base"] = "pr1"
    hub = Hub(repo, prs, events)
    monkeypatch.setattr(batch, "_github", hub)
    info = batch._pr_info(repo, 2, "main", allow_merged=True, stacked=True)
    batch._merge_pr(repo, 2, info, "merge", "main", git(repo, "rev-parse", "origin/main"))
    assert prs[2]["base"] == "main"
    assert hub.merge_argv[-1][-2:] == ["--match-head-commit", prs[2]["head"]]
    assert "--admin" not in hub.merge_argv[-1]


def test_stacked_run_pr_refused_for_rebase_or_squash(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[2]["base"] = "pr1"
    run = {"id": "run-9", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo, base: (run, [("t1", 1), ("t2", 2)]))
    data = batch._manifest(m)
    data["lanes"] = [{"id": "run-lane", "run": "run-9", "version_bump": True}]
    data["merge_method"] = "squash"
    with pytest.raises(Refused, match="batch-stacked-method|stacked run PRs need merge_method"):
        batch._entries(data, object(), repo)
    assert events == []


def test_first_failed_lane_stops_before_next_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    before = git(repo, "rev-parse", "origin/main")
    real_suite = batch._run_full

    def suite(*args):
        events.append("suite")
        return real_suite(*args)

    def checks(run, number):
        events.append(f"checks:{number}")
        if number == 1:
            raise Refused("checks-failed", "no green checks")

    monkeypatch.setattr(batch, "_run_full", suite)
    monkeypatch.setattr(batch.land, "_wait_checks", checks)
    with pytest.raises(Refused, match="A:"):
        batch.land_batch(str(m), cwd=repo)
    assert events == ["suite", "checks:1"]
    assert git(repo, "rev-parse", "origin/main") == before
    status = json.loads(batch._state_path(repo, "release-a").read_text())
    assert status["status"] == "blocked" and status["failed_lane"]["id"] == "A"


def test_compose_conflict_refuses_before_suite_or_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[4] = pr_branch(repo, 4, {"a.txt": "clash\n"})
    data = json.loads(m.read_text())
    data["lanes"].append({"id": "C", "pr": 4, "land_after": ["B"]})
    m.write_text(json.dumps(data))
    before = git(repo, "rev-parse", "origin/main")
    monkeypatch.setattr(batch, "_run_full", lambda *a: pytest.fail("suite ran on a conflicting composition"))
    with pytest.raises(Refused, match="does not merge cleanly"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []
    assert git(repo, "rev-parse", "origin/main") == before
    assert not git(repo, "worktree", "list").count("office-batch-compose")


def test_unreviewed_tree_change_after_merges_blocks_green(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    monkeypatch.setattr(batch, "_github", Hub(repo, prs, events, extra="unreviewed.txt"))
    with pytest.raises(Refused, match="suite-validated tree"):
        batch.land_batch(str(m), cwd=repo)
    status = json.loads(batch._state_path(repo, "release-a").read_text())
    assert status["status"] == "blocked" and status["failed_lane"] == {"id": "A", "reason": "tree"}


def test_head_moved_during_suite_refuses_its_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    real_suite = batch._run_full

    def suite(repo_, sha, manifest, folder):
        result = real_suite(repo_, sha, manifest, folder)
        git(repo, "switch", "pr2")
        (repo / "late.txt").write_text("pushed after validation\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "push after validation")
        prs[2]["head"] = git(repo, "rev-parse", "HEAD")
        git(repo, "switch", "main")
        return result

    monkeypatch.setattr(batch, "_run_full", suite)
    with pytest.raises(Refused, match="changed head"):
        batch.land_batch(str(m), cwd=repo)
    assert events == ["checks:1", "merge:1", "checks:2"]


def test_main_moved_while_suite_ran_recomposes_next_time(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    real_suite = batch._run_full

    def suite(repo_, sha, manifest, folder):
        result = real_suite(repo_, sha, manifest, folder)
        git(repo, "switch", "main")
        (repo / "external.txt").write_text("external\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "external")
        git(repo, "push", "origin", "main")
        return result

    monkeypatch.setattr(batch, "_run_full", suite)
    with pytest.raises(Refused, match="moved while the full suite ran"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []
    assert json.loads(batch._state_path(repo, "release-a").read_text())["status"] == "pending"
    monkeypatch.setattr(batch, "_run_full", real_suite)
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"


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
    before = git(repo, "rev-parse", "origin/main")
    with pytest.raises(Refused, match="full suite exited 1"):
        batch.land_batch(str(m), cwd=repo)
    assert len(suite_calls) == 1
    assert git(repo, "rev-parse", "origin/main") == before
    with pytest.raises(Refused, match="append an ordered fix"):
        batch.land_batch(str(m), cwd=repo)
    assert len(suite_calls) == 1
    data = json.loads(m.read_text())
    data["lanes"].append({"id": "fix", "pr": 3, "land_after": ["B"]})
    m.write_text(json.dumps(data))
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"
    assert events == ["suite", "suite", "checks:1", "merge:1", "checks:2", "merge:2", "checks:3", "merge:3"]
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
    with pytest.raises(Refused, match="single version bump to 3.15.0 not present in src/office/_version.py"):
        batch.land_batch(str(m), cwd=repo)
    assert git(repo, "rev-parse", "origin/main") == before
    assert events == []


def test_run_lane_accepts_stacked_prs_with_parent_base(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[2]["base"] = "pr1"
    run = {"id": "run-123", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo, base: (run, [("t1", 1), ("t2", 2)]))
    data = batch._manifest(m)
    data["lanes"] = [{"id": "run-lane", "run": "run-123", "version_bump": True}]
    entries, infos = batch._entries(data, object(), repo)
    assert entries[0]["prs"] == [1, 2]
    assert entries[0]["tasks"] == {1: "t1", 2: "t2"}
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

def test_cli_routing_without_active_run(tmp_path, monkeypatch, capsys):
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
    capsys.readouterr()
    assert cli.main(["land", "--batch", "release.json", "--merge"]) == 2
    assert "batch-flags" in capsys.readouterr().out
    assert cli.main(["land", "--retry-full"]) == 2
    assert "require --batch" in capsys.readouterr().out


def _alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _wait_dead(pid: int, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_suite_runs_with_an_allowlisted_environment_only(tmp_path, monkeypatch):
    repo = fixture_repo(tmp_path)
    planted = {"GH_TOKEN": "ghp_leak", "GITHUB_REPOSITORY": "x/y", "DB_PASSWORD": "pw", "MY_SECRET": "s",
               "API_KEY": "k", "AWS_REGION": "us-east-1", "SSH_AUTH_SOCK": "/tmp/agent", "OFFICE_DISPATCH_ID": "D1",
               "GPG_KEY_ID": "ABC", "DATABASE_URL": "postgres://u:pw@h/db", "github_token": "lower"}
    for name, value in planted.items():
        monkeypatch.setenv(name, value)
    script = tmp_path / "dump_env.sh"
    script.write_text('env > "$1"\n')
    dump = tmp_path / "env.txt"
    manifest = {"id": "env-check", "timeout_seconds": 60, "full_suite": ["sh", str(script), str(dump)]}
    code, _ = batch._run_full(repo, git(repo, "rev-parse", "origin/main"), manifest, tmp_path / "batches")
    assert code == 0
    names = {line.split("=", 1)[0] for line in dump.read_text().splitlines() if "=" in line}
    assert not (names & set(planted))
    assert {"PATH", "HOME"} <= names
    assert f"HOME={Path.home()}" not in dump.read_text()


def test_suite_timeout_kills_its_whole_process_group(tmp_path):
    repo = fixture_repo(tmp_path)
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "hang.sh"
    script.write_text('sleep 30 & echo $! > "$1"\nwait\n')
    manifest = {"id": "timeout-check", "timeout_seconds": 5, "full_suite": ["sh", str(script), str(pidfile)]}
    started = time.monotonic()
    code, log = batch._run_full(repo, git(repo, "rev-parse", "origin/main"), manifest, tmp_path / "batches")
    assert code == 124 and time.monotonic() - started < 30
    assert "timed out" in log.read_text()
    assert _wait_dead(int(pidfile.read_text()))


def test_background_child_does_not_outlive_a_passing_suite(tmp_path):
    repo = fixture_repo(tmp_path)
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "leave.sh"
    script.write_text('sleep 30 & echo $! > "$1"\nexit 0\n')
    manifest = {"id": "leave-check", "timeout_seconds": 60, "full_suite": ["sh", str(script), str(pidfile)]}
    code, _ = batch._run_full(repo, git(repo, "rev-parse", "origin/main"), manifest, tmp_path / "batches")
    assert code == 0
    assert _wait_dead(int(pidfile.read_text()))


def test_suite_that_cannot_start_is_a_red_result_not_a_traceback(tmp_path):
    repo = fixture_repo(tmp_path)
    manifest = {"id": "nostart", "timeout_seconds": 60, "full_suite": ["/nonexistent/suite-binary"]}
    code, log = batch._run_full(repo, git(repo, "rev-parse", "origin/main"), manifest, tmp_path / "batches")
    assert code == 127 and "could not start" in log.read_text()


def test_each_suite_attempt_keeps_its_own_log(tmp_path):
    repo = fixture_repo(tmp_path)
    sha = git(repo, "rev-parse", "origin/main")
    script = tmp_path / "fail.sh"
    script.write_text("exit 1\n")
    manifest = {"id": "logs", "timeout_seconds": 60, "full_suite": ["sh", str(script)]}
    first = batch._run_full(repo, sha, manifest, tmp_path / "batches")[1]
    second = batch._run_full(repo, sha, manifest, tmp_path / "batches")[1]
    assert first != second
    assert {p.name for p in (tmp_path / "batches" / "logs").iterdir()} == {f"full-{sha}-1.log", f"full-{sha}-2.log"}


def test_compose_ignores_commit_signing_and_hooks(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    git(repo, "config", "commit.gpgsign", "true")
    git(repo, "config", "gpg.program", "false")
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"


def test_stacked_run_pr_base_must_belong_to_its_run(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[2]["base"] = "unrelated"
    run = {"id": "run-5", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo, base: (run, [("t1", 1), ("t2", 2)]))
    data = batch._manifest(m)
    data["lanes"] = [{"id": "run-lane", "run": "run-5", "version_bump": True}]
    with pytest.raises(Refused, match="not part of run lane"):
        batch._entries(data, object(), repo)


def test_run_integration_changed_after_validation_blocks_its_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    monkeypatch.setattr(batch.land, "_land_lock", lambda run: contextlib.nullcontext())
    monkeypatch.setattr(batch.land, "_snapshot", lambda con, run: {"commit": "b"})
    run = {"id": "run-7", "repo_root": str(repo)}
    head = git(repo, "rev-parse", "origin/main")
    entries = [{"lane": {"id": "A"}, "run": run, "prs": [1], "tasks": {1: "t1"}}]
    infos = {1: batch._pr_info(repo, 1, "main", allow_merged=True, stacked=True)}
    record = {"start": head, "composed": head, "completed": [], "heads": {"1": prs[1]["head"]},
              "run_snapshots": {"A": {"commit": "a"}}, "trees": {}}
    with pytest.raises(Refused, match="integration changed"):
        batch._merge_lanes(object(), repo, record, batch._state_path(repo, "release-a"),
                           batch._manifest(m), entries, infos, Result(lines=[]))
    assert events == []


def test_blocked_batch_with_no_merges_recomposes_on_fixed_heads(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)

    def failing(run, number):
        events.append(f"checks:{number}")
        if number == 1:
            raise Refused("checks-failed", "no green checks")

    monkeypatch.setattr(batch.land, "_wait_checks", failing)
    with pytest.raises(Refused, match="A:"):
        batch.land_batch(str(m), cwd=repo)
    git(repo, "switch", "pr1")
    (repo / "a.txt").write_text("core fixed\n")
    git(repo, "commit", "-am", "fix the failing check")
    prs[1]["head"] = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    monkeypatch.setattr(batch.land, "_wait_checks", lambda run, number: events.append(f"checks:{number}"))
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"
    assert result.data["batch"]["heads"]["1"] == prs[1]["head"]


def test_dated_changelog_heading_counts_as_the_version_header(tmp_path):
    repo = fixture_repo(tmp_path)
    start = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "office" / "_version.py").write_text('__version__ = "3.15.0"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 3.15.0 (2026-10-31)\n\n- batch\n\n## 3.14.0\n\n- initial\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "bump")
    manifest = {"version": "3.15.0", "version_file": "src/office/_version.py", "changelog_file": "CHANGELOG.md"}
    batch._verify_version(repo, start, git(repo, "rev-parse", "HEAD"), manifest)


def test_manifest_refuses_duplicate_keys(tmp_path):
    p = tmp_path / "batch.json"
    p.write_text('{"id": "a", "id": "b", "version": "3.15.0", "full_suite": ["true"], '
                 '"lanes": [{"id": "A", "pr": 1, "version_bump": true}]}')
    with pytest.raises(Usage, match="duplicate manifest keys: id"):
        batch._manifest(p)


def test_restart_inside_a_multi_pr_run_lane_resumes(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    run = {"id": "run-9", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo, base: (run, [("t1", 1), ("t2", 2)]))
    data = json.loads(m.read_text())
    data["lanes"] = [{"id": "R", "run": "run-9", "version_bump": True}]
    m.write_text(json.dumps(data))

    class FakeCon:
        def close(self):
            pass

    monkeypatch.setattr(batch.paths, "runs_db", lambda: m)
    monkeypatch.setattr("office.db.connect", lambda: FakeCon())
    monkeypatch.setattr(batch.land, "_snapshot", lambda con, run: {"commit": "x"})
    monkeypatch.setattr(batch.land, "_land_lock", lambda run: contextlib.nullcontext())
    monkeypatch.setattr(batch, "_record_run_landings", lambda *args, **kwargs: None)
    crashes = {"left": 1}

    def mark(con, run, task_id, number, base, method):
        events.append(f"mark:{number}")
        if crashes["left"]:
            crashes["left"] = 0
            raise RuntimeError("process interrupted after the first PR merged")

    monkeypatch.setattr(batch, "_mark_task_merged", mark)
    with pytest.raises(RuntimeError):
        batch.land_batch(str(m), cwd=repo)
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"
    assert [e for e in events if e.startswith("mark")] == ["mark:1", "mark:1", "mark:2"]



# ---- Test-strength coverage: each guard below has a test that fails when it is removed. ----

def test_rerun_after_partial_merge_skips_merged_lanes(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    suites = []
    real_suite = batch._run_full

    def counted(*args):
        suites.append(args[1])
        return real_suite(*args)

    failing = {"on": True}

    def checks(run, number):
        events.append(f"checks:{number}")
        if number == 2 and failing["on"]:
            raise Refused("checks-failed", "no green checks on #2")

    monkeypatch.setattr(batch, "_run_full", counted)
    monkeypatch.setattr(batch.land, "_wait_checks", checks)
    with pytest.raises(Refused, match="B:"):
        batch.land_batch(str(m), cwd=repo)
    status = json.loads(batch._state_path(repo, "release-a").read_text())
    assert status["completed"] == ["A"] and status["status"] == "blocked"
    failing["on"] = False
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"
    assert "A: already merged; skipped" in result.lines
    assert events.count("merge:1") == 1 and events.count("merge:2") == 1
    assert len(suites) == 1


def test_head_moved_after_block_is_refused_before_resumed_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    failing = {"on": True}

    def checks(run, number):
        events.append(f"checks:{number}")
        if number == 2 and failing["on"]:
            raise Refused("checks-failed", "no green checks on #2")

    monkeypatch.setattr(batch.land, "_wait_checks", checks)
    with pytest.raises(Refused, match="B:"):
        batch.land_batch(str(m), cwd=repo)
    git(repo, "switch", "pr2")
    (repo / "late.txt").write_text("fix pushed after the block\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fix")
    prs[2]["head"] = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    failing["on"] = False
    with pytest.raises(Refused, match="changed head after the suite validated"):
        batch.land_batch(str(m), cwd=repo)
    assert "merge:2" not in events


def test_main_moved_between_lanes_stops_before_next_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    real_save = batch._save

    def save_then_push_external(path, data):
        real_save(path, data)
        # Runs right after lane A is recorded as completed, before lane B starts.
        if data.get("completed") == ["A"] and data.get("status") == "merging":
            git(repo, "switch", "main")
            git(repo, "fetch", "origin", "main")
            git(repo, "reset", "--hard", "origin/main")
            (repo / "external.txt").write_text("external\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "external push between lanes")
            git(repo, "push", "origin", "main")

    monkeypatch.setattr(batch, "_save", save_then_push_external)
    with pytest.raises(Refused, match="moved outside this batch"):
        batch.land_batch(str(m), cwd=repo)
    assert events == ["checks:1", "merge:1"]


def test_draft_version_owner_is_refused_before_any_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[2]["draft"] = True
    with pytest.raises(Refused, match="is a draft"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_closed_pr_is_refused_before_any_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[1]["state"] = "CLOSED"
    with pytest.raises(Refused, match="expected an open PR"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_pr_targeting_another_base_is_refused(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[1]["base"] = "elsewhere"
    with pytest.raises(Refused, match="targets elsewhere, expected main"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_missing_changelog_header_is_refused_before_suite_or_merge(tmp_path, monkeypatch):
    owner = owner_files()
    owner["CHANGELOG.md"] = "# Changelog\n\n## 3.14.0\n\n- initial\n"
    repo, prs, events, m = setup_two(tmp_path, monkeypatch, owner=owner)
    monkeypatch.setattr(batch, "_run_full", lambda *a: pytest.fail("suite ran without a changelog header"))
    with pytest.raises(Refused, match="needs exactly one 3.15.0 header"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_version_must_increase_from_the_starting_version(tmp_path, monkeypatch):
    owner = {"src/office/_version.py": '__version__ = "3.13.9"\n',
             "CHANGELOG.md": "# Changelog\n\n## 3.13.9\n\n- batch\n\n## 3.14.0\n\n- initial\n"}
    repo, prs, events, m = setup_two(tmp_path, monkeypatch, owner=owner)
    data = json.loads(m.read_text())
    data["version"] = "3.13.9"
    m.write_text(json.dumps(data))
    with pytest.raises(Refused, match="version must increase"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_second_concurrent_batch_is_refused_by_the_repository_lock(tmp_path, monkeypatch):
    import fcntl
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    lock_path = batch._state_path(repo, "release-a").parent.parent / "landing.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(Refused, match="another batch landing is active"):
            batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_changed_stable_key_after_red_suite_is_refused(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    monkeypatch.setattr(batch, "_run_full", lambda repo, sha, manifest, folder: (1, folder / "red.log"))
    with pytest.raises(Refused, match="full suite exited 1"):
        batch.land_batch(str(m), cwd=repo)
    data = json.loads(m.read_text())
    data["full_suite"] = ["true"]
    m.write_text(json.dumps(data))
    with pytest.raises(Refused, match="changed since batch start"):
        batch.land_batch(str(m), cwd=repo)


def test_appending_lanes_to_a_green_batch_is_refused(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"
    data = json.loads(m.read_text())
    data["lanes"].append({"id": "C", "pr": 3, "land_after": ["B"]})
    m.write_text(json.dumps(data))
    with pytest.raises(Refused, match="a green batch cannot be extended"):
        batch.land_batch(str(m), cwd=repo)


def test_new_batch_containing_a_merged_pr_is_refused(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[1]["state"] = "MERGED"
    with pytest.raises(Refused, match="cannot start a new batch containing previously merged PRs"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_retry_full_reruns_the_suite_on_an_unchanged_composition(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    verdicts = iter([1, 0])
    suites = []

    def suite(repo_, sha, manifest, folder):
        suites.append(sha)
        code = next(verdicts)
        log = folder / f"full-{len(suites)}.log"
        log.write_text("x\n")
        return code, log

    monkeypatch.setattr(batch, "_run_full", suite)
    with pytest.raises(Refused, match="full suite exited 1"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []
    result = batch.land_batch(str(m), cwd=repo, retry_full=True)
    assert result.data["batch"]["status"] == "green"
    assert len(suites) == 2 and suites[0] == suites[1]
    assert events == ["checks:1", "merge:1", "checks:2", "merge:2"]


def test_duplicate_pr_across_lanes_is_refused_before_any_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    data = json.loads(m.read_text())
    data["lanes"].append({"id": "C", "pr": 2, "land_after": ["A"]})
    m.write_text(json.dumps(data))
    with pytest.raises(Refused, match="appears in more than one lane"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_worker_session_cannot_land_a_batch(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "D1")
    with pytest.raises(Refused, match="a worker cannot land a batch"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_suite_that_pushes_is_refused_before_it_runs(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    data = json.loads(m.read_text())
    data["full_suite"] = ["git", "push", "origin", "HEAD:main"]
    m.write_text(json.dumps(data))
    before = git(repo, "rev-parse", "origin/main")
    with pytest.raises(Refused, match="must not push or mutate a remote"):
        batch.land_batch(str(m), cwd=repo)
    assert git(repo, "rev-parse", "origin/main") == before
    assert events == []


def test_timed_out_merge_blocks_the_lane(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)

    def slow(repo_, number, info, method, base, expected):
        raise subprocess.TimeoutExpired("gh", 5)

    monkeypatch.setattr(batch, "_merge_pr", slow)
    with pytest.raises(Refused, match="A:"):
        batch.land_batch(str(m), cwd=repo)
    status = json.loads(batch._state_path(repo, "release-a").read_text())
    assert status["status"] == "blocked" and status["failed_lane"]["id"] == "A"


def test_pr_head_missing_locally_is_fetched_from_the_pull_ref(tmp_path):
    repo = fixture_repo(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True)
    git(other, "config", "user.name", "Batch Test")
    git(other, "config", "user.email", "batch@example.invalid")
    (other / "remote-only.txt").write_text("only on the pull ref\n")
    git(other, "add", ".")
    git(other, "commit", "-m", "remote only")
    sha = git(other, "rev-parse", "HEAD")
    git(other, "push", "-q", "origin", "HEAD:refs/pull/5/head")
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0
    batch._fetch_pr_head(repo, 5, sha)
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0
    with pytest.raises(Refused, match="batch-pr-head-unavailable|cannot inspect the pinned head"):
        batch._fetch_pr_head(repo, 6, "0" * 40)


MANIFEST_REFUSALS = [
    (lambda m: m["lanes"][0].update(run="run-x"), "supply exactly one of pr or run"),
    (lambda m: m["lanes"][0].update(pr=0), "pr must be a positive integer"),
    (lambda m: m.update(timeout_seconds=30), "timeout_seconds must be between"),
    (lambda m: m.update(merge_method="bogus"), "merge_method must be one of"),
    (lambda m: m.update(base="../x"), "base must name a simple local branch"),
    (lambda m: m.update(version="3.15"), "version must be an explicit"),
    (lambda m: m.update(version_file="../x"), "cannot escape the repository"),
    (lambda m: m.update(changelog_file=".git/config"), "cannot escape the repository"),
    (lambda m: m.update(version_file="/etc/passwd"), "must be a relative repository file"),
    (lambda m: m["lanes"][1].update(land_after="A"), "land_after must be a list"),
    (lambda m: m["lanes"][0].update(version_bump="yes"), "version_bump must be true or false"),
    (lambda m: m.update(full_suite="true && false"), "not a shell pipeline"),
]


@pytest.mark.parametrize("change,reason", MANIFEST_REFUSALS)
def test_manifest_refusals_name_their_reason(tmp_path, change, reason):
    obj = {"id": "release", "version": "3.15.0", "full_suite": ["true"],
           "lanes": [{"id": "A", "pr": 1}, {"id": "B", "pr": 2, "version_bump": True}]}
    change(obj)
    p = tmp_path / "batch.json"
    p.write_text(json.dumps(obj))
    with pytest.raises(Usage, match=re.escape(reason)):
        batch._manifest(p)


def _run_lane_stubs(monkeypatch, repo, *, run_repo=None, mode="merge", enabled=True,
                    base="main", pr=1, found=True):
    run = {"id": "run-z", "repo_root": str(run_repo or repo)}
    monkeypatch.setattr(batch.state, "find_run", lambda con, ident: run if found else None)
    monkeypatch.setattr(batch.land, "end_state", lambda con, r: {"mode": mode})
    monkeypatch.setattr(batch.land, "_snapshot", lambda con, r: {"commit": "c", "accepted": []})
    monkeypatch.setattr(batch.land, "_require_run_coverage", lambda r, commit, con=None: None)
    monkeypatch.setattr(batch.prs, "settings", lambda con, r: {"enabled": enabled, "base_branch": base})
    monkeypatch.setattr(batch.integration, "_topo", lambda tasks: tasks)
    monkeypatch.setattr(batch.integration, "accepted_set", lambda con, r: [{"id": "t1"}])
    monkeypatch.setattr(batch.prs, "has_pr", lambda task: True)
    monkeypatch.setattr(batch.state, "get_task", lambda con, rid, tid: {"pr": {"number": pr}})


@pytest.mark.parametrize("overrides,reason", [
    ({"run_repo": "/elsewhere"}, "run belongs to"),
    ({"mode": "deploy"}, "approved merge end_state"),
    ({"enabled": False}, "PRs are disabled or the run uses another base"),
    ({"base": "develop"}, "PRs are disabled or the run uses another base"),
    ({"pr": None}, "accepted task has no PR"),
    ({"found": False}, "not found"),
])
def test_run_lane_refuses_each_unlandable_state(tmp_path, monkeypatch, overrides, reason):
    repo = fixture_repo(tmp_path)
    _run_lane_stubs(monkeypatch, repo, **overrides)
    with pytest.raises(Refused, match=re.escape(reason)):
        batch._run_lane(object(), {"id": "R", "run": "run-z"}, repo, "main")


def test_run_lane_merge_marks_each_task_pr_and_records_the_landing(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    data = json.loads(m.read_text())
    data["lanes"] = [{"id": "R", "run": "run-e", "version_bump": True}]
    m.write_text(json.dumps(data))
    run = {"id": "run-e", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo_, base: (run, [("t1", 1), ("t2", 2)]))
    monkeypatch.setattr(batch.paths, "runs_db", lambda: m)
    monkeypatch.setattr("office.db.connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr("office.db.transaction", lambda con: contextlib.nullcontext())
    monkeypatch.setattr(batch.land, "_snapshot", lambda con, r: {"commit": "c"})
    monkeypatch.setattr(batch.land, "_land_lock", lambda r: contextlib.nullcontext())
    tasks = {"t1": {"pr": {"number": 1}}, "t2": {"pr": {"number": 2}}}
    updates, recorded = [], []
    monkeypatch.setattr(batch.state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(batch.state, "update_task",
                        lambda con, rid, tid, **fields: updates.append((tid, fields["pr"])))
    monkeypatch.setattr(batch.state, "emit", lambda *a, **k: None)
    monkeypatch.setattr(batch.state, "get_run", lambda con, rid: {"id": rid, "landing": {}})
    monkeypatch.setattr(batch.land, "_record", lambda con, r, **fields: recorded.append(fields))
    start = git(repo, "rev-parse", "origin/main")
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"
    assert [tid for tid, _ in updates] == ["t1", "t2"]
    assert all(fields["merged"] and fields["base"] == "main" for _, fields in updates)
    assert len(recorded) == 1 and recorded[0]["merged"]["rollback"] == start


def test_main_moved_during_a_pr_checks_wait_stops_its_merge(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)

    def checks(run, number):
        events.append(f"checks:{number}")
        if number == 2:
            # External push lands while PR #2 waits for its checks, after the lane-level check passed.
            git(repo, "switch", "main")
            git(repo, "fetch", "origin", "main")
            git(repo, "reset", "--hard", "origin/main")
            (repo / "external.txt").write_text("external during checks\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "external during checks")
            git(repo, "push", "origin", "main")

    monkeypatch.setattr(batch.land, "_wait_checks", checks)
    with pytest.raises(Refused, match="moved while #2 waited for its checks"):
        batch.land_batch(str(m), cwd=repo)
    assert "merge:2" not in events


def test_compose_ignores_operator_merge_hooks(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    hook = repo / ".git" / "hooks" / "pre-merge-commit"
    # Fails only inside the throwaway compose checkout, so the fake GitHub merge in the repo still works.
    hook.write_text('#!/bin/sh\ncase "$PWD" in *office-batch-compose*) exit 1;; esac\nexit 0\n')
    hook.chmod(0o755)
    git(repo, "config", "core.hooksPath", str(repo / ".git" / "hooks"))
    result = batch.land_batch(str(m), cwd=repo)
    assert result.data["batch"]["status"] == "green"


def _stub_run_lane(monkeypatch, repo, m, snapshot):
    data = json.loads(m.read_text())
    data["lanes"] = [{"id": "R", "run": "run-e", "version_bump": True}]
    m.write_text(json.dumps(data))
    run = {"id": "run-e", "repo_root": str(repo)}
    monkeypatch.setattr(batch, "_run_lane", lambda con, lane, repo_, base: (run, [("t1", 1), ("t2", 2)]))
    monkeypatch.setattr(batch.paths, "runs_db", lambda: m)
    monkeypatch.setattr("office.db.connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr("office.db.transaction", lambda con: contextlib.nullcontext())
    monkeypatch.setattr(batch.land, "_snapshot", snapshot)
    monkeypatch.setattr(batch.land, "_land_lock", lambda r: contextlib.nullcontext())
    tasks = {"t1": {"pr": {"number": 1}}, "t2": {"pr": {"number": 2}}}
    updates, recorded = [], []
    monkeypatch.setattr(batch.state, "get_task", lambda con, rid, tid: tasks[tid])
    monkeypatch.setattr(batch.state, "update_task", lambda con, rid, tid, **fields: updates.append((tid, fields["pr"])))
    monkeypatch.setattr(batch.state, "emit", lambda *a, **k: None)
    monkeypatch.setattr(batch.state, "get_run", lambda con, rid: {"id": rid, "landing": {}})
    monkeypatch.setattr(batch.land, "_record", lambda con, r, **fields: recorded.append(fields))
    return updates, recorded


def test_run_lane_snapshot_with_tuples_still_merges(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    _stub_run_lane(monkeypatch, repo, m, lambda con, r: {"commit": "c", "gates": (("g", "pass"),)})
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"


def test_run_integration_changing_during_suite_recomposes(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    box = {"c": "c1"}
    _stub_run_lane(monkeypatch, repo, m, lambda con, r: {"commit": box["c"]})

    def suite(repo_, sha, manifest, folder):
        box["c"] = "c2"  # the run's integration changes while the suite runs
        return 0, folder / "x.log"

    monkeypatch.setattr(batch, "_run_full", suite)
    with pytest.raises(Refused, match="a run's integration changed"):
        batch.land_batch(str(m), cwd=repo)
    status = json.loads(batch._state_path(repo, "release-a").read_text())
    assert status["status"] == "pending" and status["validated"] is None
    assert events == []


def test_retry_full_after_run_changed_recomposes_instead_of_reattesting(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    box = {"c": "c1"}
    _stub_run_lane(monkeypatch, repo, m, lambda con, r: {"commit": box["c"]})
    monkeypatch.setattr(batch, "_run_full", lambda repo_, sha, manifest, folder: (1, folder / "red.log"))
    with pytest.raises(Refused, match="full suite exited 1"):
        batch.land_batch(str(m), cwd=repo)
    box["c"] = "c2"
    with pytest.raises(Refused, match="integration changed since the composition"):
        batch.land_batch(str(m), cwd=repo, retry_full=True)
    assert json.loads(batch._state_path(repo, "release-a").read_text())["status"] == "pending"
    assert events == []


def test_lost_post_merge_observation_recovers_on_rerun(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    real_git = batch._git
    failing = {"on": True}

    def flaky(repo_, *args):
        if failing["on"] and args == ("rev-parse", "origin/main") and events[-1:] == ["merge:2"]:
            failing["on"] = False
            raise Refused("batch-fetch-failed", "network dropped after the merge")
        return real_git(repo_, *args)

    monkeypatch.setattr(batch, "_git", flaky)
    with pytest.raises(Refused, match="network dropped"):
        batch.land_batch(str(m), cwd=repo)
    assert json.loads(batch._state_path(repo, "release-a").read_text())["status"] == "blocked"
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"
    assert events.count("merge:1") == 1 and events.count("merge:2") == 1


def test_crash_after_last_merge_then_main_moves_refuses_green(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    real_save = batch._save
    crashed = {"on": True}

    def save(path, data):
        if crashed["on"] and data.get("status") == "green":
            crashed["on"] = False
            git(repo, "switch", "main")
            git(repo, "fetch", "origin", "main")
            git(repo, "reset", "--hard", "origin/main")
            (repo / "external.txt").write_text("external\n")
            git(repo, "add", ".")
            git(repo, "commit", "-m", "external after the last merge")
            git(repo, "push", "origin", "main")
            raise RuntimeError("interrupted before the green record was saved")
        real_save(path, data)

    monkeypatch.setattr(batch, "_save", save)
    with pytest.raises(RuntimeError):
        batch.land_batch(str(m), cwd=repo)
    with pytest.raises(Refused, match="is not the validated result of this batch"):
        batch.land_batch(str(m), cwd=repo)
    assert json.loads(batch._state_path(repo, "release-a").read_text())["status"] == "blocked"


def test_validated_batch_that_merged_nothing_recomposes_when_main_moved():
    manifest = {"id": "r", "base": "main", "version": "3.15.0", "version_file": "src/office/_version.py",
                "changelog_file": "CHANGELOG.md", "full_suite": ["true"], "merge_method": "merge",
                "lanes": [{"id": "A", "pr": 1}]}
    record = {k: manifest[k] for k in ("base", "version", "version_file", "changelog_file", "full_suite", "merge_method")}
    record.update(start="a" * 40, lanes=manifest["lanes"], completed=[], status="validated",
                  validated={"sha": "x"}, composed="c" * 40, trees={"A": "t"}, run_snapshots={})
    out = batch._resume(record, manifest, "b" * 40)
    assert out["status"] == "pending" and out["validated"] is None


def test_record_from_before_composed_checks_is_refused():
    manifest = {"id": "r", "base": "main", "version": "3.15.0", "version_file": "src/office/_version.py",
                "changelog_file": "CHANGELOG.md", "full_suite": ["true"], "merge_method": "merge",
                "lanes": [{"id": "A", "pr": 1}]}
    record = {k: manifest[k] for k in ("base", "version", "version_file", "changelog_file", "full_suite", "merge_method")}
    record.update(start="a" * 40, lanes=manifest["lanes"], completed=["A"], status="merging", composed="c" * 40)
    with pytest.raises(Refused, match="predates the composed-tree checks"):
        batch._resume(record, manifest, "a" * 40)


def test_corrupt_batch_record_is_refused_not_a_traceback(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    path = batch._state_path(repo, "release-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    with pytest.raises(Refused, match="unreadable"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_stacked_pr_is_retargeted_only_after_its_checks_pass(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    _stub_run_lane(monkeypatch, repo, m, lambda con, r: {"commit": "c"})
    prs[2]["base"] = "pr1"
    seen = []
    monkeypatch.setattr(batch.land, "_wait_checks",
                        lambda run, number: seen.append(prs[2]["base"]) if number == 2 else None)
    assert batch.land_batch(str(m), cwd=repo).data["batch"]["status"] == "green"
    assert seen == ["pr1"] and prs[2]["base"] == "main"


@pytest.mark.parametrize("sha", ["", "abc", "g" * 40])
def test_malformed_pr_head_is_refused_before_merge(tmp_path, monkeypatch, sha):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    prs[1]["head"] = sha
    with pytest.raises(Refused, match="no readable head SHA"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


def test_unrelated_history_fails_compose_without_calling_it_a_conflict(tmp_path, monkeypatch):
    repo, prs, events, m = setup_two(tmp_path, monkeypatch)
    git(repo, "switch", "--orphan", "pr3")
    (repo / "orphan.txt").write_text("unrelated\n")
    git(repo, "add", "orphan.txt")
    git(repo, "commit", "-m", "unrelated history")
    prs[3] = {"head": git(repo, "rev-parse", "HEAD"), "files": [{"path": "orphan.txt"}],
              "state": "OPEN", "base": "main", "draft": False}
    git(repo, "switch", "main")
    data = json.loads(m.read_text())
    data["lanes"].append({"id": "C", "pr": 3, "land_after": ["B"]})
    m.write_text(json.dumps(data))
    with pytest.raises(Refused, match="merging #3 failed"):
        batch.land_batch(str(m), cwd=repo)
    assert events == []


@pytest.mark.parametrize("heading,counts", [
    ("## 3.15.0", True),
    ("## [3.15.0] - 2026-10-31", True),
    ("## v3.15.0", True),
    ("##\n\n3.15.0", False),
])
def test_changelog_heading_forms(tmp_path, heading, counts):
    repo = fixture_repo(tmp_path)
    start = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "office" / "_version.py").write_text('__version__ = "3.15.0"\n')
    (repo / "CHANGELOG.md").write_text(f"# Changelog\n\n{heading}\n\n- batch\n\n## 3.14.0\n\n- initial\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "bump")
    manifest = {"version": "3.15.0", "version_file": "src/office/_version.py", "changelog_file": "CHANGELOG.md"}
    head = git(repo, "rev-parse", "HEAD")
    if counts:
        batch._verify_version(repo, start, head, manifest)
    else:
        with pytest.raises(Refused, match="needs exactly one 3.15.0 header"):
            batch._verify_version(repo, start, head, manifest)


def test_suite_log_and_run_snapshot_survive_a_real_red_then_retry(tmp_path):
    repo = fixture_repo(tmp_path)
    sha = git(repo, "rev-parse", "origin/main")
    script = tmp_path / "fail.sh"
    script.write_text("echo red\nexit 1\n")
    manifest = {"id": "logs2", "timeout_seconds": 60, "full_suite": ["sh", str(script)]}
    _, log = batch._run_full(repo, sha, manifest, tmp_path / "batches")
    assert log.read_text().endswith("red\n") and log.stat().st_mode & 0o777 == 0o600
