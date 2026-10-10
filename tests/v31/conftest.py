"""Isolated Office environment for 3.1 tests.

Every test gets its own runs.db, state home, user config, git repository and
fake harness binaries. Nothing touches the real ~/.local or any real harness.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

import fake_agent

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


class _Pipe:
    """The write end of an in-process agent's stdin: it only keeps the prompt."""

    def __init__(self):
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    def close(self) -> None:
        pass


class _Interrupted(BaseException):
    """A signal reached the supervisor while an in-process agent was running."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class InProcessAgent:
    """A Popen-like fake harness child that runs `fake_agent.run` in this process.

    The agent runs when its output is first read, after the supervisor has
    written the prompt. It cannot be signalled as a process, so `poll()` never
    reports it as running (the supervisor's handler would otherwise forward to
    its own group). A SIGTERM/SIGHUP/SIGINT that arrives meanwhile stops the
    agent where it is, and it ends as a killed child does: return code -signum.
    """

    def __init__(self, argv, cwd, env, stdin):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.pid = os.getpid()
        self.returncode = None
        self.stdin = _Pipe() if stdin == subprocess.PIPE else None
        self.stdout = self
        self._output = None
        self._pending = []
        self._running = False
        # From the spawn on, a signal is chained to the supervisor's own handler (which records it for the
        # classification) and either stops the running agent or, before it starts, keeps it from starting.
        self._previous = {s: signal.signal(s, self._on_signal) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}

    def _on_signal(self, signum, frame):
        handler = self._previous.get(signum)
        if callable(handler):
            handler(signum, frame)
        self._pending.append(signum)
        if self._running:
            raise _Interrupted(signum)

    def _run(self):
        if self._output is not None:
            return
        prompt = self.stdin.data.decode() if self.stdin else ""
        try:
            if self._pending:  # a signal arrived before the agent started: it never runs
                raise _Interrupted(self._pending[0])
            self._running = True
            self.returncode, output = fake_agent.run(self.argv, prompt, self.env, self.cwd)
        except _Interrupted as stop:
            self.returncode, output = -stop.signum, b""
        self._running = False
        self._output = io.BytesIO(output)
        self._restore()

    def _restore(self):
        for s, handler in self._previous.items():
            signal.signal(s, handler)

    def read1(self, size=-1) -> bytes:
        self._run()
        return self._output.read1(size)

    def poll(self):
        return 0 if self.returncode is None else self.returncode

    def wait(self):
        self._run()
        return self.returncode


class Env:
    def __init__(self, tmp: Path, monkeypatch, *, restored: bool = False, contract: str | None = None):
        """`restored`: tmp already holds a repository copied from a snapshot. `contract`
        pins the review contract new runs start with (the user config's review.contract)."""
        self.approved = False
        self.trust_snapshots = None  # routes `trust()` has already worked out, by inputs; the env fixture sets it
        self.tmp = tmp
        self.home = tmp / "home"
        self.data = tmp / "data"
        self.state = tmp / "state"
        self.bin = tmp / "bin"
        self.repo = tmp / "repo"
        self.scenario = tmp / "scenario.json"
        for d in (self.home, self.data, self.state, self.bin):
            d.mkdir(parents=True, exist_ok=True)
        self.fakes = {}
        for name in ("codex", "claude", "gemini", "agy"):
            self.fakes[self.bin / name] = (f"#!{sys.executable}\nimport os, runpy\nos.environ['FAKE_HARNESS'] = '{name}'\n"
                                           f"runpy.run_path({str(FAKE)!r}, run_name='__main__')\n")
            (self.bin / name).write_text(self.fakes[self.bin / name])
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
        # A held startup prompt waits for an answer no test gives unless it asks to (#510).
        monkeypatch.setenv("OFFICE_STARTUP_PROMPT_WAIT", "0")
        monkeypatch.setenv("FAKE_SCENARIO", str(self.scenario))
        # Harness session transcripts (the reviewer-reply fallback) are read from
        # these homes; never from the real ones.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(self.home / ".claude"))
        monkeypatch.setenv("CODEX_HOME", str(self.home / ".codex"))
        # Only fake harnesses and system tools: a real harness on PATH must never run in tests.
        system = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        monkeypatch.setenv("PATH", os.pathsep.join([str(self.bin)] + system))
        pythonpath = [str(SRC)]
        import site
        if site.ENABLE_USER_SITE and hasattr(site, "USER_BASE") and Path(site.USER_BASE).exists():
            monkeypatch.setenv("PYTHONUSERBASE", site.USER_BASE)
        for p in sys.path:
            if "site-packages" in p and p not in pythonpath and Path(p).is_dir():
                pythonpath.append(p)
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(pythonpath))
        monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
        self.scenario.write_text("{}")
        if contract:
            # A suite about v3.1 review policy runs v3.1 runs, as a run started before #337 does.
            (tmp / "user-config.yaml").write_text(f"review:\n  contract: {contract}\n")
        if not restored:
            self._init_repo()
        sys.path.insert(0, str(SRC))
        from office import dispatch
        from office.util import claim_identity
        # A disposable state home: reporters working it are reclaimed when this
        # owner process ends, even if the directory survives an interrupted
        # teardown (#501).
        monkeypatch.setenv("OFFICE_STATE_HOME_OWNER", claim_identity(os.getpid()))
        spawn_process = dispatch._spawn_agent
        monkeypatch.setattr(dispatch, "_spawn_agent", lambda argv, cwd, env, stdin: self._spawn_agent(
            spawn_process, argv, cwd, env, stdin))

    def _spawn_agent(self, spawn_process, argv, cwd, env, stdin):
        """The agent seam: an unmodified fake harness runs in this process; anything
        that needs a real process (a real binary, a sleep, a wall cap, a signal
        the scenario cannot emulate) is started as one."""
        exe = shutil.which(argv[0], path=env.get("PATH"))
        fake = exe and Path(exe) in self.fakes and Path(exe).read_text() == self.fakes[Path(exe)]
        if not fake or self._has_wall_cap(Path(exe).name, env) or not fake_agent.scenario_runs_in_process(env):
            return spawn_process(argv, cwd, env, stdin)
        env = {**env, "FAKE_HARNESS": Path(exe).name}
        return InProcessAgent(argv, cwd, env, stdin)

    @staticmethod
    def _has_wall_cap(harness, env):
        """Whether the supervisor would arm a wall-clock cap for this harness: the env override or
        any of its profiles' own `max_minutes`."""
        from office import adapters, dispatch
        with mock.patch.dict(os.environ, {k: v for k, v in env.items() if k == "OFFICE_WORKER_MAX_MINUTES"}):
            return any(dispatch._wall_cap_seconds(prof or {}) for adapter in adapters.load_all().values()
                       if adapters.executable(adapter) == harness
                       for prof in (adapter.get("office_profiles") or {}).values())

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
        """Run `office <args>` in this process under `cwd` and the `env` overrides.

        Returns (exit code, stdout+stderr). Work the command hands to other
        processes (workers, reviewers, checks) still runs as real subprocesses.
        """
        if "OFFICE_VERSION_OVERRIDE" in (env or {}):
            return self._office_subprocess(args, cwd, env, check)
        from office import cli
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(out))
            stack.enter_context(mock.patch.dict(os.environ, env or {}))
            stack.enter_context(mock.patch.object(sys, "argv", ["office", *args]))
            stack.enter_context(contextlib.chdir(cwd or self.repo))
            try:
                code = cli.main(list(args))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
        if check is not None and code != check:
            raise AssertionError(f"office {' '.join(args)} exit {code} (want {check}):\n{out.getvalue()}")
        return code, out.getvalue()

    def _office_subprocess(self, args, cwd, env, check):
        """A real `python -m office`, for behavior that replaces the process (the
        front door re-executes under a run's pinned release)."""
        proc = subprocess.run([sys.executable, "-m", "office", *args], cwd=str(cwd or self.repo), capture_output=True,
                              text=True, env={**os.environ, **(env or {})})
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

    def _trust_inputs(self):
        """Everything `trust()` derives its routes from. Calls with equal inputs record the same triples."""
        from office import adapters, candidates, routing, scoring
        from office.util import sha256_obj
        all_adapters = adapters.load_all()
        catalog = candidates.catalog_rows()
        user_config = Path(os.environ["OFFICE_USER_CONFIG"])
        found = {}  # what each adapter finds on PATH: an untouched fake of ours (by name), or whatever else it is
        for name, adapter in all_adapters.items():
            exe = shutil.which(adapters.executable(adapter) or "")
            fake = exe and Path(exe) in self.fakes and Path(exe).read_text() == self.fakes[Path(exe)]
            found[name] = Path(exe).name if fake else exe
        # Route identity includes the resolved harness major, which can change
        # while the executable path and adapter configuration stay the same.
        route_harnesses = {row.get("invocation_harness") for row in catalog
                           if row.get("dispatchable") is not False}
        versions = {name: scoring.harness_major(adapters.harness_version(all_adapters[name]))
                    for name in route_harnesses
                    if name in all_adapters and found.get(name)}
        data = sha256_obj([all_adapters, catalog, found, versions,
                           user_config.read_text() if user_config.is_file() else None])
        # a test that replaces any of these functions gets its own routes
        return (data, candidates.build_candidates, candidates.catalog_rows, adapters.load_all, adapters.installed,
                adapters.harness_version, routing.candidate_id, scoring.record_trust_act)

    def trust(self, triple_prefix: str = ""):
        """Record the user's explicit trust acts for every installed fake route.

        The first call per module and inputs (`trust_snapshots`, set for tests that are not `approved`)
        builds the candidates and keeps the acts it recorded. Later calls restore those acts in one
        transaction: same triples, states, actors and reasons, a fresh id and time for each.
        """
        sys.path.insert(0, str(SRC))
        from office import candidates, paths, routing, scoring
        key = None
        if self.trust_snapshots is not None and not triple_prefix:
            key = self._trust_inputs()
        acts = self.trust_snapshots.get(key) if key else None
        if acts is not None:
            con = self.con()
            try:
                scoring.ensure_trust_schema(con)
                for act in acts:
                    con.execute("INSERT INTO adapter_trust_acts VALUES (?,?,?,?,?,?,?)", (
                        str(uuid.uuid4()), act["triple"], act["target_state"], act["actor_id"], act["reason"],
                        act["evidence_reference"], datetime.now(timezone.utc).isoformat()))
                con.commit()
            finally:
                con.close()
            return
        acts = []
        con = self.con()
        try:
            for role in ("executor", "code_reviewer", "visual_reviewer", "integration_reviewer"):
                cands, _ = candidates.build_candidates(con, role, probe=False)
                for c in cands:
                    acts.append(scoring.record_trust_act(paths.runs_db(), routing.candidate_id(c), "proven", "user",
                                                         "test fixture: user trusts the fake route"))
        finally:
            con.close()
        if key:
            self.trust_snapshots[key] = acts

    def write_plan(self, text: str, where: Path | None = None, run_id: str | None = None):
        """Write the plan draft of run_id (default: the newest run)."""
        if run_id is None:
            con = self.con()
            try:
                run_id = con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
            finally:
                con.close()
        d = (where or self.repo) / ".office" / "plans" / run_id.split("-", 1)[0][:8]
        d.mkdir(parents=True, exist_ok=True)
        (d / "PLAN.md").write_text(text)

    def calls(self):
        log = self.scenario.with_suffix(".log")
        return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def _activate(e: Env, monkeypatch) -> Env:
    """Point this process at `e`: its repo as cwd, fresh version and quota caches."""
    monkeypatch.chdir(e.repo)
    sys.path.insert(0, str(SRC))
    from office import candidates, version
    version.current.cache_clear()
    candidates._QUOTA_CACHE.clear()
    return e


def _contract(request) -> str | None:
    """The review contract a `review_contract("v3.1")` marker pins, if any."""
    m = request.node.get_closest_marker("review_contract")
    return m.args[0] if m else None


@pytest.fixture(scope="module")
def _approved_snapshot(tmp_path_factory, request):
    """The state `trust + start + approve plan` for PLAN_ONE, built once per module.

    The steps cost about a second each time; tests marked `approved` restore this
    copy instead. Paths inside Office state are absolute, so every restore lands
    at the same directory the snapshot was built in.
    """
    root = tmp_path_factory.mktemp("approved")
    monkeypatch = pytest.MonkeyPatch()
    try:
        e = _activate(Env(root / "env", monkeypatch, contract=_contract(request)), monkeypatch)
        approved_run(e)
        shutil.copytree(e.tmp, root / "snapshot", symlinks=True)
    finally:
        monkeypatch.undo()
    return root / "env", root / "snapshot"


@pytest.fixture(scope="module")
def _trust_snapshots():
    """The routes `Env.trust()` records, worked out by the first call of each module and inputs."""
    return {}


@pytest.fixture
def env(request, tmp_path, monkeypatch):
    """An isolated Env. With `@pytest.mark.approved`, one whose default plan is already approved."""
    if request.node.get_closest_marker("approved"):
        live, snapshot = request.getfixturevalue("_approved_snapshot")
        shutil.rmtree(live, ignore_errors=True)
        shutil.copytree(snapshot, live, symlinks=True)
        e = Env(live, monkeypatch, restored=True, contract=_contract(request))
        e.approved = True
    else:
        e = Env(tmp_path, monkeypatch, contract=_contract(request))
        e.trust_snapshots = request.getfixturevalue("_trust_snapshots")
    try:
        yield _activate(e, monkeypatch)
    finally:
        # Release the disposable home explicitly; its reporter leaves with it (#501).
        shutil.rmtree(e.state, ignore_errors=True)


def start_inline(env, plan=PLAN_ONE, gear="direct+review", extra=()):
    code, out = env.office("start", "fixture goal", "--gear", gear, "--planner", "inline", *extra)
    assert code == 0, out
    env.write_plan(plan)
    code, out = env.office("submit")
    assert code == 0, out
    return out


def approved_run(env, plan=PLAN_ONE, gear="direct+review", **script):
    """Trust the fake routes, script the harnesses by role, start an inline run and approve its plan.

    An Env from a test marked `approved` already holds that state for the default
    plan and gear; only the scripting is left to do.
    """
    if env.approved:
        assert (plan, gear) == (PLAN_ONE, "direct+review"), "an approved Env holds the default plan and gear"
        env.script(**script)
        return
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan, gear=gear)
    env.office("approve", "plan", "--quote", "approved", check=0)


def task_row(env, tid="T1") -> dict:
    con = env.con()
    try:
        return dict(con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    finally:
        con.close()


def write_clean_ledger(wt) -> str:
    """Write the clean self-review ledger naming the worktree's HEAD (#421). Returns that HEAD."""
    head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    lenses = "\n".join(f"LENS {n} reviewed" for n in ("security", "edge-cases", "platform", "test-strength"))
    (Path(wt) / "OFFICE_SELF_REVIEW.md").write_text(f"COMMIT {head}\nROUND 1\n{lenses}\n")
    return head


def self_reviewed(wt, *files: str) -> str:
    """Commit `files` in the task worktree and write the clean self-review ledger `office submit` now requires
    for substantive in-scope work (#421). Returns the HEAD the ledger names."""
    def git(*a):
        return subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@e.test", "-c", "user.name=t", *a],
                              check=True, capture_output=True, text=True).stdout.strip()
    git("add", "--", *files)
    git("commit", "-qm", "work")
    return write_clean_ledger(wt)
