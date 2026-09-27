#!/usr/bin/env python3
"""Drive the matched v3 vs v3.1 prospective evaluation (spec §24).

One job = one fixture x one version x one repetition. Each job gets a fresh seed
repository and isolated Office homes, an identical orchestrator (`codex exec
--json`, same model and effort, the version's own SKILL.md as its only Office
guidance) and identical scenario events. The driver records a timestamped event
transcript, worker invocations (through a PATH shim that logs and then execs the
real harness), the hidden acceptance result and the per-version invariants.

The Claude harness is withheld from PATH for both versions (the user's Claude
session quota is protected); the evaluation policy routes every role to Codex.
A quota guard pauses scheduling while Codex headroom is low.

    eval/v31/run.py --fixtures all --versions v3,v31 --reps 3 --parallel 3
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fixtures import BASE_FILES, COMMON_RULES, EVAL_POLICY, FIXTURES  # noqa: E402

EVAL_HOME = Path(os.environ.get("OFFICE_EVAL_HOME", "~/.office-eval")).expanduser()
PLUGINS = {"v3": EVAL_HOME / "v3-plugin", "v31": EVAL_HOME / "v31-plugin"}
ORCHESTRATOR_MODEL = os.environ.get("OFFICE_EVAL_ORCHESTRATOR", "gpt-5.6-sol")
ORCHESTRATOR_EFFORT = os.environ.get("OFFICE_EVAL_ORCHESTRATOR_EFFORT", "medium")
REAL_CODEX = shutil.which("codex")
LOCAL_BIN = Path.home() / ".local" / "bin"
# Worker models that are not the executor in EVAL_POLICY (planner, reviewers).
NON_EXECUTOR_MODELS = ("gpt-6-astra", " astra", "gpt-5.6-luna", " luna")
QUOTA_FLOOR_SESSION = float(os.environ.get("OFFICE_EVAL_MIN_SESSION", "15"))
QUOTA_FLOOR_WEEKLY = float(os.environ.get("OFFICE_EVAL_MIN_WEEKLY", "40"))
_quota_lock = threading.Lock()
JOB_TIMEOUT = int(os.environ.get("OFFICE_EVAL_TIMEOUT", "4500"))

SHIM = r'''#!/bin/sh
# Evaluation shim: log the invocation, then run the real harness.
printf '{"t": %s, "harness": "%s", "cwd": "%s", "argv": "%s"}\n' "$(python3 -c 'import time;print(time.time())')" \
  "{name}" "$PWD" "$(printf '%s ' "$@" | head -c 400 | tr -d '"\\\n')" >> "{log}"
{block}
exec "{real}" "$@"
'''

UNAVAILABLE = r'''case " $* " in
  *"{model}"*) echo "ERROR: You've hit your usage limit for {model}. Upgrade or try again later. (quota exhausted)" >&2; exit 1;;
esac'''


def now() -> float:
    return time.time()


def sh(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def seed_repo(repo: Path, fixture: dict, plugin: Path) -> None:
    repo.mkdir(parents=True)
    for rel, text in {**BASE_FILES, **fixture.get("files", {})}.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (repo / ".auto-office").mkdir()
    (repo / ".auto-office" / "config.yaml").write_text(EVAL_POLICY)
    sh(["git", "init", "-q", "-b", "main"], cwd=repo)
    sh(["git", "-c", "user.name=eval", "-c", "user.email=eval@local", "add", "-A"], cwd=repo)
    sh(["git", "-c", "user.name=eval", "-c", "user.email=eval@local", "commit", "-q", "-m", "seed"], cwd=repo)


def make_shims(root: Path, fixture: dict) -> Path:
    shim_dir = root / "shims"
    shim_dir.mkdir()
    log = root / "harness-calls.jsonl"
    event = fixture.get("event") or {}
    # Everything the user has in ~/.local/bin except the Claude harness (withheld)
    # and the harnesses that get a logging shim below.
    for exe in LOCAL_BIN.iterdir():
        if exe.name.startswith(("claude", "herdr", "codex", "agy", "gemini")) or not os.access(exe, os.X_OK):
            continue
        (shim_dir / exe.name).symlink_to(exe)
    for name in ("codex", "agy", "gemini"):
        real = shutil.which(name)
        if not real:
            continue
        block = UNAVAILABLE.replace("{model}", event["model"]) if event.get("kind") == "shim_unavailable" else ""
        text = SHIM.replace("{name}", name).replace("{log}", str(log)).replace("{real}", real).replace("{block}", block)
        (shim_dir / name).write_text(text)
        (shim_dir / name).chmod(0o755)
    # Pane-hosted workers would not inherit the isolated homes, so panes are off
    # for both versions in the evaluation.
    (shim_dir / "herdr").write_text("#!/bin/sh\necho 'herdr is unavailable in this evaluation' >&2\nexit 1\n")
    (shim_dir / "herdr").chmod(0o755)
    return shim_dir


def job_env(root: Path, version: str, shim_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OFFICE_", "AUTO_OFFICE_", "HERDR_", "CLAUDE_CODE_", "CLAUDECODE"))}
    path = [str(shim_dir)]
    if version == "v31":
        path.append(str(PLUGINS["v31"] / ".venv" / "bin"))
        env.update(OFFICE_DATA_HOME=str(root / "data"), OFFICE_STATE_HOME=str(root / "state"))
    else:
        env.update(XDG_STATE_HOME=str(root / "xdg-state"), AUTO_OFFICE_RUNS_DB=str(root / "v3-runs.db"))
    nvm = sorted(Path.home().glob(".nvm/versions/node/*/bin"))
    rest = [p for p in env.get("PATH", "").split(os.pathsep) if p and Path(p).expanduser() != LOCAL_BIN]
    env["PATH"] = os.pathsep.join(path + [str(b) for b in nvm[-1:]] + rest)
    # The orchestrator runs every command through a login shell, which would
    # rebuild PATH from the user's profile (putting ~/.local/bin, and so the
    # real harnesses, first). A private ZDOTDIR re-applies the job PATH after
    # macOS path_helper and skips the user's zsh configuration for both versions.
    zdot = root / "zdot"
    zdot.mkdir(exist_ok=True)
    line = f"export PATH={shlex.quote(env['PATH'])}\n"
    for name in (".zshenv", ".zprofile", ".zshrc"):
        (zdot / name).write_text(line)
    env["ZDOTDIR"] = str(zdot)
    env["SHELL"] = "/bin/zsh"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class Orchestrator:
    """A `codex exec --json` session with a timestamped transcript. A user
    message mid-run is delivered as an interactive user would: interrupt, then
    resume the same thread with the message."""

    def __init__(self, root: Path, repo: Path, env: dict, plugin: Path, tag: str, t0: float):
        self.root, self.repo, self.env, self.plugin, self.tag, self.t0 = root, repo, env, plugin, tag, t0
        self.out = open(root / f"transcript-{tag}.jsonl", "a")
        self.thread_id = None
        self.proc = None
        self.lock = threading.Lock()

    def _note(self, **fields) -> None:
        with self.lock:
            self.out.write(json.dumps({"t": now() - self.t0, **fields}) + "\n")
            self.out.flush()

    def _launch(self, argv: list[str], prompt: str) -> None:
        self.proc = subprocess.Popen(argv, cwd=self.repo, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=open(self.root / f"orchestrator-{self.tag}.stderr", "a"), text=True,
                                     start_new_session=True)
        self.proc.stdin.write(prompt)
        self.proc.stdin.close()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _flags(self) -> list[str]:
        return ["--json", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check",
                "-m", ORCHESTRATOR_MODEL, "-c", f'model_reasoning_effort="{ORCHESTRATOR_EFFORT}"']

    def start(self, prompt: str) -> None:
        self._note(driver="user_message", text=prompt[:300])
        self._launch([REAL_CODEX, "exec", *self._flags(), "--cd", str(self.repo), "-"], prompt)

    def send(self, text: str) -> None:
        """Interrupt the running turn and resume the same thread with `text`."""
        self._note(driver="user_message", text=text[:300], via="interrupt+resume")
        if self.proc and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGINT)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.kill()
        if not self.thread_id:
            self._note(driver="resume_failed", reason="no thread id")
            return
        # resume rejects --cd and inherits the caller's cwd (Popen cwd=repo).
        self._launch([REAL_CODEX, "exec", "resume", *self._flags(), self.thread_id, "-"], text)

    def _read(self, proc) -> None:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                self.thread_id = self.thread_id or event["thread_id"]
            self._note(e=event)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._note(driver="killed")


def first_executor_call(root: Path, t0: float) -> float | None:
    """Launch time of the first worker that is not the planner or a reviewer."""
    log = root / "harness-calls.jsonl"
    if not log.exists():
        return None
    for line in log.read_text().splitlines():
        try:
            call = json.loads(line)
        except ValueError:
            continue
        argv = " " + call.get("argv", "")
        if not argv.strip() or argv.split()[0] in ("--version", "-V", "login", "models", "--help"):
            continue
        if any(m in argv for m in NON_EXECUTOR_MODELS):
            continue
        return call["t"] - t0
    return None


QUOTA_ABORT_AT = float(os.environ.get("OFFICE_EVAL_ABORT_AT", "8"))


def codex_session_left() -> float | None:
    probe = PLUGINS["v31"] / "scripts" / "codex-usage.py"
    try:
        return float(json.loads(sh([sys.executable, str(probe), "--json"], timeout=60).stdout)["session_remaining_percent"])
    except (ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def quota_gate() -> str | None:
    """Wait while the Codex 5-hour window is low; None to proceed, or a reason to skip."""
    probe = PLUGINS["v31"] / "scripts" / "codex-usage.py"
    with _quota_lock:
        while True:
            try:
                q = json.loads(sh([sys.executable, str(probe), "--json"], timeout=60).stdout)
            except (ValueError, subprocess.SubprocessError):
                return None
            if q.get("weekly_remaining_percent", 100) < QUOTA_FLOOR_WEEKLY:
                return f"weekly codex quota {q['weekly_remaining_percent']}% < {QUOTA_FLOOR_WEEKLY}%"
            if q.get("session_remaining_percent", 100) >= QUOTA_FLOOR_SESSION:
                return None
            wait = max(60, int(q.get("session_resets_in_seconds", 600)) + 60)
            print(f"quota guard: codex 5h window {q['session_remaining_percent']}% left; waiting {wait}s", flush=True)
            time.sleep(wait)


def prompt_for(fixture: dict, version: str) -> str:
    return (f"You are the Auto Office orchestrator. Your operating instructions are the Auto Office skill at "
            f"{PLUGINS[version]}/SKILL.md: read it first and follow it (paths in it are relative to that directory). "
            f"Carry out this request end to end.\n\nRequest:\n{fixture['goal']}\n{COMMON_RULES}")


def pre_event(fixture: dict, version: str, repo: Path, env: dict, root: Path) -> dict:
    """Create the unrelated active run for F08 with the version's own CLI."""
    event = fixture.get("event") or {}
    if event.get("kind") != "second_run":
        return {}
    if version == "v31":
        argv = ["office", "start", "--goal", event["goal"], "--gear", "direct"]
    else:
        argv = [sys.executable, str(PLUGINS["v3"] / "scripts" / "office_runtime.py"), "start", "--goal", event["goal"],
                "--playbook", "Change", "--gear", "direct", "--repo", str(repo)]
    proc = sh(argv, cwd=repo, env=env, timeout=600)
    (root / "second-run.txt").write_text(proc.stdout + proc.stderr)
    return {"second_run_exit": proc.returncode}


def hidden_acceptance(root: Path, repo: Path, fixture: dict) -> dict:
    """Run the hidden tests against local main (the landing target for both versions)."""
    check = root / "acceptance"
    if check.exists():
        shutil.rmtree(check)
    sh(["git", "clone", "-q", "--branch", "main", str(repo), str(check)])
    head = sh(["git", "-C", str(check), "rev-parse", "HEAD"]).stdout.strip()
    seed = sh(["git", "-C", str(check), "rev-list", "--max-parents=0", "HEAD"]).stdout.strip()
    (check / "tests" / "test_hidden_acceptance.py").write_text(fixture["hidden"])
    proc = sh([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_hidden_acceptance.py"],
              cwd=check, timeout=300, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
    return {"landed": head != seed, "hidden_pass": proc.returncode == 0, "hidden_summary": tail[0][:200]}


def run_job(fixture_id: str, version: str, rep: int, results: Path) -> dict:
    fixture = FIXTURES[fixture_id]
    plugin = PLUGINS[version]
    skip = quota_gate()
    if skip:
        with open(results.with_suffix(".skipped.jsonl"), "a") as fh:
            fh.write(json.dumps({"fixture": fixture_id, "version": version, "rep": rep, "skipped": skip}) + "\n")
        raise RuntimeError(f"skipped: {skip}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = EVAL_HOME / "runs" / f"{fixture_id}-{version}-r{rep}-{stamp}"
    root.mkdir(parents=True)
    repo = root / "repo"
    seed_repo(repo, fixture, plugin)
    shim_dir = make_shims(root, fixture)
    env = job_env(root, version, shim_dir)
    record = {"fixture": fixture_id, "version": version, "rep": rep, "root": str(root),
              "orchestrator": f"codex/{ORCHESTRATOR_MODEL}@{ORCHESTRATOR_EFFORT}", "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    db = root / "data" / "runs.db" if version == "v31" else root / "v3-runs.db"
    py = str(PLUGINS["v31"] / ".venv" / "bin" / "python")  # has PyYAML; v3 needs only the stdlib + yaml
    seeded = sh([py, str(HERE / "seed_baseline.py"), version, str(plugin), str(db),
                 str(EVAL_HOME.parent / ".office-live" / "data" / "runs.db")], env=env, timeout=120)
    record["baseline"] = (seeded.stdout or seeded.stderr).strip()[-200:]
    record.update(pre_event(fixture, version, repo, env, root))
    v3_scratch_before = {p.name for p in (Path.home() / ".office" / "worktrees").glob("*")} if version == "v3" else set()
    t0 = now()
    sessions = [Orchestrator(root, repo, env, plugin, "s1", t0)]
    sessions[0].start(prompt_for(fixture, version))
    event = fixture.get("event") or {}
    deadline = t0 + JOB_TIMEOUT
    fired = False
    last_quota = now()
    while now() < deadline:
        cur = sessions[-1]
        if not cur.alive():
            break
        if now() - last_quota > 120:
            last_quota = now()
            left = codex_session_left()
            if left is not None and left <= QUOTA_ABORT_AT:
                # Never spend the user's protected reserve on the evaluation.
                record["aborted"] = f"codex 5h window at {left}% (reserve guard {QUOTA_ABORT_AT}%)"
                break
        if not fired and event.get("after") == "first_executor":
            t_exec = first_executor_call(root, t0)
            if t_exec is not None and now() - t0 >= t_exec + event["delay"]:
                fired = True
                record["event_fired_at"] = round(now() - t0, 1)
                if event["kind"] == "amend":
                    cur.send(event["text"])
                elif event["kind"] == "interrupt":
                    cur.kill()
                    time.sleep(5)
                    nxt = Orchestrator(root, repo, env, plugin, "s2", t0)
                    nxt.start(event["resume"] + f" The skill is at {plugin}/SKILL.md.\n" + COMMON_RULES)
                    sessions.append(nxt)
        time.sleep(2)
    timed_out = now() >= deadline
    for s_ in sessions:
        if s_.alive():
            s_.kill()
    record["wall_seconds"] = round(now() - t0, 1)
    record["timed_out"] = timed_out
    record["time_to_first_executor"] = first_executor_call(root, t0)
    record["fired"] = fired if event.get("after") else None
    time.sleep(3)
    record.update(hidden_acceptance(root, repo, fixture))
    if version == "v3":
        created = {p.name for p in (Path.home() / ".office" / "worktrees").glob("*")} - v3_scratch_before
        (root / "v3-scratch-created.json").write_text(json.dumps(sorted(created)))
    with open(results, "a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default="all")
    ap.add_argument("--versions", default="v3,v31")
    ap.add_argument("--reps", default="1", help="count (3) or explicit list (4,5)")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--results", default=str(EVAL_HOME / "results.jsonl"))
    args = ap.parse_args()
    fixtures = list(FIXTURES) if args.fixtures == "all" else args.fixtures.split(",")
    reps = [int(r) for r in args.reps.split(",")] if "," in args.reps else list(range(1, int(args.reps) + 1))
    jobs = [(f, v, r) for r in reps for f in fixtures for v in args.versions.split(",")]
    for v in args.versions.split(","):
        if not PLUGINS[v].is_dir():
            print(f"missing plugin checkout {PLUGINS[v]}", file=sys.stderr)
            return 2
    print(f"{len(jobs)} jobs, parallel {args.parallel}", flush=True)
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(run_job, f, v, r, Path(args.results)): (f, v, r) for f, v, r in jobs}
        for fut in futures:
            f, v, r = futures[fut]
            try:
                rec = fut.result()
                print(f"done {f} {v} r{r}: wall={rec['wall_seconds']}s ttfd={rec['time_to_first_executor']} "
                      f"landed={rec['landed']} hidden={rec['hidden_pass']}", flush=True)
            except Exception as exc:  # a broken job must not stop the matrix
                print(f"error {f} {v} r{r}: {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
