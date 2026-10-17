"""#457: deploy cwd reporting, warnings for deploy paths a fresh checkout lacks, and `deploy.env_files`."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from conftest import start_inline
from office import land
from office.state import Refused
from test_land import _plan, _run


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / ".gitignore").write_text(".env\nmyaccount/.env\ndist/\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "deploy.sh").write_text("echo deploy\n")
    (repo / "tracked.txt").write_text("t\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    (repo / ".env").write_text("TOKEN=1\n")
    (repo / "myaccount").mkdir()
    (repo / "myaccount" / ".env").write_text("TOKEN=2\n")
    return repo


# ---------------------------------------------------------------- warnings

def test_a_gitignored_env_file_in_a_deploy_command_is_warned(repo):
    out = land.deploy_path_warnings(repo, {"deploy_prod": "set -a && . ./.env && vercel deploy --prod"})
    assert len(out) == 1 and "deploy_prod references .env, which is gitignored" in out[0], out


def test_a_path_passed_to_a_sourced_script_or_flag_is_warned(repo):
    out = land.deploy_path_warnings(repo, {"deploy_prod": "source scripts/deploy.sh myaccount/.env --env-file=.env"})
    assert [w.split(" references ")[1].split(",")[0] for w in out] == ["myaccount/.env", ".env"], out


def test_an_untracked_file_is_warned_as_untracked(repo):
    (repo / "scripts" / "secrets.sh").write_text("x\n")
    out = land.deploy_path_warnings(repo, {"deploy_preview": "bash scripts/secrets.sh"})
    assert len(out) == 1 and "scripts/secrets.sh, which is untracked" in out[0], out


def test_tracked_missing_url_and_variable_words_are_not_warned(repo):
    cmd = "bash scripts/deploy.sh tracked.txt nothere.env https://x.test/.env $HOME/.env '*.env' -f"
    assert land.deploy_path_warnings(repo, {"deploy_prod": cmd}) == []


def test_files_listed_in_env_files_are_not_warned(repo):
    cmd = "bash scripts/deploy.sh .env myaccount/.env"
    assert land.deploy_path_warnings(repo, {"deploy_prod": cmd}, [".env"]) == \
        land.deploy_path_warnings(repo, {"deploy_prod": "bash scripts/deploy.sh myaccount/.env"})
    assert land.deploy_path_warnings(repo, {"deploy_prod": cmd}, [".env", "myaccount/.env"]) == []


def test_paths_outside_the_repo_are_not_warned(repo, tmp_path):
    (tmp_path / "outside.env").write_text("x")
    assert land.deploy_path_warnings(repo, {"deploy_prod": f"cat ../outside.env {tmp_path}/outside.env"}) == []


def test_detect_warns_about_paths_in_deploy_scripts(repo):
    (repo / "package.json").write_text(json.dumps({"scripts": {"deploy": "vercel deploy --prod --env-file=.env"}}))
    res = land.detect_deploy(repo)
    assert any(line.startswith("warning: package.json script deploy references .env, which is gitignored")
               for line in res.lines), res.lines
    assert len(res.data["warnings"]) == 1


def test_office_land_detect_prints_the_warning(env):
    (env.repo / ".gitignore").write_text(".env\n")
    (env.repo / "package.json").write_text(json.dumps({"scripts": {"deploy": "vercel deploy --env-file=.env"}}))
    env.git("add", "-A")
    env.git("commit", "-qm", "ignore env")
    (env.repo / ".env").write_text("T=1\n")
    code, out = env.office("land", "--detect")
    assert code == 0 and "warning: package.json script deploy references .env, which is gitignored" in out, out


def test_start_with_deploy_flags_warns_and_listing_the_file_silences_it(env):
    (env.repo / ".gitignore").write_text(".env\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "ignore env")
    (env.repo / ".env").write_text("T=1\n")
    env.trust()
    code, out = env.office("start", "g", "--planner", "inline", "--deploy-prod", "sh -c '. ./.env && deploy'")
    assert code == 0 and "warning: deploy_prod references .env, which is gitignored" in out, out
    code, out = env.office("start", "g2", "--planner", "inline", "--deploy-prod", "sh -c '. ./.env && deploy'",
                           "--set", "deploy.env_files=[.env]")
    assert code == 0 and "references .env" not in out, out


def test_plan_submit_warns_about_a_gitignored_deploy_path(env):
    (env.repo / ".gitignore").write_text(".env\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "ignore env")
    (env.repo / ".env").write_text("T=1\n")
    env.trust()
    plan = _plan("preview", preview="sh -c '. ./.env && deploy'", verify="true")
    out = start_inline(env, plan=plan)
    assert "deploy_preview references .env, which is gitignored" in out, out


# ---------------------------------------------------------------- copying

def test_env_files_are_copied_with_their_mode(repo, tmp_path):
    (repo / ".env").chmod(0o640)
    checkout = tmp_path / "c"
    (checkout / "myaccount").mkdir(parents=True)
    copied = land.copy_env_files(repo, checkout, [".env", "myaccount/.env", "absent.env"])
    assert copied == [".env", "myaccount/.env"]
    assert (checkout / ".env").read_text() == "TOKEN=1\n"
    assert stat.S_IMODE((checkout / ".env").stat().st_mode) == 0o640
    assert (checkout / "myaccount" / ".env").read_text() == "TOKEN=2\n"


def test_a_single_string_entry_is_one_env_file():
    assert land.env_files({"deploy": {"env_files": ".env"}}) == [".env"]
    assert land.env_files({"deploy": {"env_files": [".env", "a/.env"]}}) == [".env", "a/.env"]
    assert land.env_files({}) == [] and land.env_files({"deploy": {"env_files": None}}) == []


def test_a_tracked_file_in_the_checkout_is_not_overwritten(repo, tmp_path):
    checkout = tmp_path / "c"
    checkout.mkdir()
    (checkout / "tracked.txt").write_text("committed\n")
    (repo / "tracked.txt").write_text("edited locally\n")
    assert land.copy_env_files(repo, checkout, ["tracked.txt"]) == []
    assert (checkout / "tracked.txt").read_text() == "committed\n"


@pytest.mark.parametrize("entry", ["../x", "a/../../x", "/etc/hosts", " "])
def test_env_files_outside_the_repo_are_refused(repo, tmp_path, entry):
    checkout = tmp_path / "c"
    checkout.mkdir()
    with pytest.raises(Refused) as exc:
        land.copy_env_files(repo, checkout, [entry])
    assert exc.value.category == "deploy-env-file-invalid"


def test_a_link_to_a_file_outside_the_repo_is_refused(repo, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("s")
    os.symlink(secret, repo / "link.env")
    checkout = tmp_path / "c"
    checkout.mkdir()
    with pytest.raises(Refused) as exc:
        land.copy_env_files(repo, checkout, ["link.env"])
    assert exc.value.category == "deploy-env-file-invalid" and not (checkout / "link.env").exists()


def test_a_checkout_directory_linked_outside_is_refused(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    checkout = tmp_path / "c"
    checkout.mkdir()
    os.symlink(outside, checkout / "myaccount")
    with pytest.raises(Refused) as exc:
        land.copy_env_files(repo, checkout, ["myaccount/.env"])
    assert exc.value.category == "deploy-env-file-invalid" and not list(outside.iterdir())


# ---------------------------------------------------------------- land

def test_land_reports_the_deploy_cwd_and_copies_env_files(env, monkeypatch):
    (env.repo / ".gitignore").write_text(".env\n")
    (env.repo / ".auto-office").mkdir()
    (env.repo / ".auto-office" / "config.yaml").write_text("deploy:\n  env_files: ['.env']\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "ignore env")
    (env.repo / ".env").write_text("TOKEN=abc\n")
    (env.repo / ".env").chmod(0o600)
    seen = env.tmp / "seen"
    cmd = f"python3 -c \"import os; open('{seen}','w').write(os.getcwd() + chr(10) + open('.env').read())\""
    _run(env, monkeypatch, _plan("preview", preview=cmd, verify="test -f .env"))
    code, out = env.office("land")
    assert code == 0, out
    cwd, content = seen.read_text().split("\n", 1)
    assert content == "TOKEN=abc\n" and "_integration" not in cwd and "checkouts" in cwd, (cwd, content)
    assert f"preview deploy ok: `{cmd}` (cwd {cwd})" in out and "copied deploy.env_files .env" in out, out
    run_dir = next(Path(env.state).rglob("deploys"))
    log = (run_dir / "preview-deploy.log").read_text()
    assert log.startswith(f"# office land preview deploy\n# cwd: {cwd}\n"), log
    con = env.con()
    payloads = [json.loads(p) for (p,) in con.execute(
        "SELECT payload_json FROM events WHERE kind IN ('land.deploy.start','land.deploy','land.verify') ORDER BY seq")]
    assert [p["cwd"] for p in payloads] == [cwd] * 3, payloads
    assert not os.path.exists(cwd)
    commit = json.loads(con.execute("SELECT landing_json FROM runs").fetchone()[0])["integration"]["commit"]
    assert ".env" not in env.git("ls-tree", "-r", "--name-only", commit).split()  # never committed


def test_without_env_files_the_deploy_checkout_has_no_ignored_file(env, monkeypatch):
    (env.repo / ".gitignore").write_text(".env\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "ignore env")
    (env.repo / ".env").write_text("TOKEN=abc\n")
    _run(env, monkeypatch, _plan("preview", preview="test -f .env"))
    code, out = env.office("land")
    assert code == 4 and "preview deploy failed (exit 1) in " in out, out


def test_an_env_file_outside_the_repo_refuses_the_land_before_deploying(env, monkeypatch):
    (env.repo / ".auto-office").mkdir()
    (env.repo / ".auto-office" / "config.yaml").write_text("deploy:\n  env_files: ['../x']\n")
    marker = env.tmp / "ran"
    _run(env, monkeypatch, _plan("preview", preview=f"touch {marker}"))
    code, out = env.office("land")
    assert code != 0 and "deploy-env-file-invalid" in out and not marker.exists(), out
