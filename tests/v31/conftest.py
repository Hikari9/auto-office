"""Isolated Office environment for 3.1 tests.

Every test gets its own runs.db, state home, user config, git repository and
fake harness binaries. Nothing touches the real ~/.local or any real harness.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FAKE = Path(__file__).resolve().parent / "fake_agent.py"

PLAN_ONE = """# Plan

## Requirements
done:
- add() returns the sum
blast_radius: repo
non_goals:
- no CLI

## Tasks
### T1: Implement add
scope: calc.py
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
visual: none
"""

PLAN_TWO = """# Plan

## Requirements
done:
- add and mul exist
blast_radius: repo

## Tasks
### T1: Implement add
scope: calc.py
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
visual: none

### T2: Implement mul
scope: mul.py
depends: none
checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"
accept:
- mul.mul(2, 3) == 6
visual: none
"""

GOOD_ADD = "def add(a, b):\n    return a + b\n"
BAD_ADD = "def add(a, b):\n    return a - b\n"
GOOD_MUL = "def mul(a, b):\n    return a * b\n"


class Env:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.home = tmp / "home"
        self.data = tmp / "data"
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.repo = tmp / "repo"
        self.scenario = tmp / "scenario.json"
        for d in (self.home, self.data, self.state, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        for name in ("codex", "claude", "gemini", "agy"):
            (self.bin / name).write_text(f"#!{sys.executable}\nimport os, runpy\nos.environ['FAKE_HARNESS'] = '{name}'\n"
                                         f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
            (self.bin / name).chmod(0o755)
        env_drop = [k for k in os.environ if k.startswith(("OFFICE_", "HERDR_", "AUTO_OFFICE_"))]
        for k in env_drop:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("OFFICE_DATA_HOME", str(self.data))
        monkeypatch.setenv("OFFICE_STATE_HOME", str(self.state))
        monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp / "user-config.yaml"))
        monkeypatch.setenv("OFFICE_JOBS", "inline")
        monkeypatch.setenv("OFFICE_LAUNCHER", "sync")
        monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
        monkeypatch.setenv("FAKE_SCENARIO", str(self.scenario))
        # Only fake harnesses and system tools: a real harness on PATH must never run in tests.
        system = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        monkeypatch.setenv("PATH", os.pathsep.join([str(self.bin)] + system))
        monkeypatch.setenv("PYTHONPATH", str(SRC))
        monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
        self.scenario.write_text("{}")
        self._init_repo()

    def _init_repo(self):
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
        (self.repo / "README.md").write_text("fixture\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], check=True, capture_output=True, text=True).stdout

    def script(self, **roles):
        self.scenario.write_text(json.dumps(roles))
        for c in self.tmp.glob("scenario.*.count"):
            c.unlink()

    def office(self, *args, cwd=None, env=None, check=None):
        e = dict(os.environ)
        e.update(env or {})
        proc = subprocess.run([sys.executable, "-m", "office", *args], cwd=str(cwd or self.repo), capture_output=True,
                              text=True, env=e)
        out = proc.stdout + proc.stderr
        if check is not None and proc.returncode != check:
            raise AssertionError(f"office {' '.join(args)} exit {proc.returncode} (want {check}):\n{out}")
        return proc.returncode, out

    def ojson(self, *args, cwd=None, env=None):
        code, out = self.office(*args, "--json", cwd=cwd, env=env)
        try:
            return code, json.loads(out[out.index("{"):])
        except ValueError:
            raise AssertionError(out)

    def con(self):
        sys.path.insert(0, str(SRC))
        from office import db
        return db.connect()

    def trust(self, triple_prefix: str = ""):
        """Record the user's explicit trust acts for every installed fake route."""
        sys.path.insert(0, str(SRC))
        from office import candidates, paths, routing, scoring
        con = self.con()
        try:
            for role in ("executor", "code_reviewer", "visual_reviewer", "integration_reviewer"):
                cands, _ = candidates.build_candidates(con, role, probe=False)
                for c in cands:
                    scoring.record_trust_act(paths.runs_db(), routing.candidate_id(c), "proven", "user",
                                             "test fixture: user trusts the fake route")
        finally:
            con.close()

    def write_plan(self, text: str, where: Path | None = None):
        d = (where or self.repo) / ".office"
        d.mkdir(exist_ok=True)
        (d / "PLAN.md").write_text(text)

    def calls(self):
        log = self.scenario.with_suffix(".log")
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.chdir(e.repo)
    sys.path.insert(0, str(SRC))
    from office import version
    version.current.cache_clear()
    from office import candidates
    candidates._QUOTA_CACHE.clear()
    return e


def start_inline(env, plan=PLAN_ONE, gear="direct+review", extra=()):
    code, out = env.office("start", "fixture goal", "--gear", gear, "--planner", "inline", *extra)
    assert code == 0, out
    env.write_plan(plan)
    code, out = env.office("submit")
    assert code == 0, out
    return out
