"""Issue-only Auto-Office self-improvement observer.

Observations are durable, independent of prunable run details. A short-lived CLI
invocation only records evidence and starts a detached, single-owner reporter.
The reporter is the only code allowed to publish, and its only remote mutation
is `gh issue create` in Hikari9/auto-office. No source checkout is modified.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from office import db, paths
from office.util import DEAD, claim_liveness, now_iso

REPO = "Hikari9/auto-office"
SELF_ENV = "OFFICE_SELF_IMPROVE_REPORTER"
# start_reporter holds the home's flock through the spawn and passes the locked
# fd to the child, so ownership never gaps between spawn and the child's own
# claim: concurrent wake-ups cannot race that gap into duplicate reporters.
LOCK_FD_ENV = "OFFICE_SELF_IMPROVE_LOCK_FD"
# Set by whoever creates a disposable state home (the test Env fixture): the
# home is reclaimed when its owner process is gone, even when the directory
# survives an interrupted teardown. Unset means a persistent user home, whose
# reporter is bounded by the lifetime cap and the per-home lock instead.
OWNER_ENV = "OFFICE_STATE_HOME_OWNER"
# Each reporter process runs for a bounded time (env-overridable). A queue that
# outlives the bound hands ownership to a fresh successor, so retries due after
# the bound are still delivered while the population stays one reporter per
# home and no process lives unbounded. Pending incidents and retry receipts are
# durable in runs.db in every case.
MAX_REPORTER_SECONDS = 900.0
REPORTER_POLL_SECONDS = 5.0
# Routine gates, user mistakes, quota limits, and external outages are not bugs.
IGNORED = ("not-ready", "close-blocked", "user-quote-required", "invalid-override", "no-task",
           "usage", "quota-probe", "user-", "plan.awaiting", "self_improve", "self-improve")
SIGNAL = re.compile(r"failed|error|refused|retry|dead|orphan|lost|blocked|stalled|unavailable|invalid|mismatch|regression", re.I)
REDACTIONS = (
    (re.compile(r"(?i)(?:gh[pousr]_|sk-[a-z0-9_-]{10,}|Bearer\s+)[A-Za-z0-9_./+=-]+"), "[redacted-secret]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"https?://\S+"), "[redacted-url]"),
    (re.compile(r"(?<!\w)(?:/Users/|/home/|/tmp/|/private/|[A-Z]:\\)[^\s,;]+"), "[redacted-path]"),
    (re.compile(r"(?i)\b[a-z0-9_.-]+/[a-z0-9_.-]+\b"), "[redacted-repository]"),
    (re.compile(r"(?i)\b(?:token|password|secret|apikey|api_key|authorization)\s*[:=]\s*\S+"), "[redacted-field]"),
    (re.compile(r'''(?i)["'](?:token|password|secret|apikey|api_key|authorization)["']\s*:\s*["'][^"']*["']'''), "[redacted-field]"),
    (re.compile(r"\b[0-9a-f]{32,}\b", re.I), "[redacted-identifier]"),
    (re.compile(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", re.I), "[redacted-identifier]"),
)


def sanitize(value: str) -> str:
    """Deterministic, conservative public capsule; NEVER publish unfiltered raw logs."""
    text = str(value).replace("\x00", " ")
    for pattern, substitute in REDACTIONS:
        text = pattern.sub(substitute, text)
    return text.replace("Hikari9/auto-office", "Auto-Office").strip()[:800]


def _key(kind: str, summary: str) -> str:
    return hashlib.sha256((kind.lower() + "\0" + sanitize(summary).lower()).encode()).hexdigest()


def _bug_signal(kind: str, summary: str) -> bool:
    if kind.startswith(("self_improve.", "self-improve.")):
        return False
    candidate = (kind + " " + summary).lower()
    return not any(k in candidate for k in IGNORED) and bool(SIGNAL.search(kind))


def _record(con, run_id: str, kind: str, summary: str, origin: str) -> None:
    if not _bug_signal(kind, summary):
        return
    text = sanitize(summary)
    key = _key(kind, text)
    con.execute("INSERT INTO self_improve_incidents(fingerprint,run_id,kind,summary,origin,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'pending',?,?) ON CONFLICT(fingerprint) DO UPDATE SET "
                "occurrences=occurrences+1,updated_at=excluded.updated_at,"
                "status=CASE WHEN status='suspected' THEN 'pending' ELSE status END,"
                "report_json=CASE WHEN status='suspected' THEN NULL ELSE report_json END,"
                "next_retry_at=CASE WHEN status='suspected' THEN NULL ELSE next_retry_at END",
                (key, run_id,kind,text,origin,now_iso(),now_iso()))


def _once(con, run_id: str, origin: str, kind: str, summary: str) -> None:
    """Persist a unique non-event source before considering it as an incident."""
    saved = con.execute("INSERT OR IGNORE INTO self_improve_seen_sources(run_id,origin) VALUES(?,?)",
                        (run_id,origin))
    if saved.rowcount:
        _record(con,run_id,kind,summary,origin)


def arm(con, run_id: str) -> None:
    with db.transaction(con):
        con.execute("INSERT INTO self_improve_runs(run_id,armed,updated_at) VALUES(?,1,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET armed=1,updated_at=excluded.updated_at", (run_id, now_iso()))


def armed(con, run_id: str) -> bool:
    row = con.execute("SELECT armed FROM self_improve_runs WHERE run_id=?", (run_id,)).fetchone()
    return bool(row and row[0])


def capture(con, run_id: str, *, force: bool = False) -> int:
    """Ingest all run events, even from subagents, exactly once per cursor.

    First land/close audit backfills from event zero. This must happen BEFORE
    cleanup/prune removes evidence. Weak candidates remain local unless supported.
    """
    with db.transaction(con):
        row = con.execute("SELECT cursor,armed FROM self_improve_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            if not force:
                return 0
            con.execute("INSERT INTO self_improve_runs(run_id,armed,updated_at) VALUES(?,0,?)", (run_id,now_iso()))
        elif not force and not row["armed"]:
            return 0
        cursor = row["cursor"] if row else 0
        rows = con.execute("SELECT seq,kind,summary,task_id,dispatch_id FROM events WHERE run_id=? AND seq>? "
                           "ORDER BY seq", (run_id,cursor)).fetchall()
        for e in rows:
            _record(con,run_id,e["kind"],e["summary"],f"event:{e['seq']}")
        # Harvest failures that never emitted an event (dead process, aborted job).
        for job in con.execute("SELECT id,kind,error FROM outbox WHERE run_id=? AND status='failed' "
                               "AND error IS NOT NULL",(run_id,)).fetchall():
            _once(con,run_id,"job:"+job["id"],"job.failed",job["kind"]+": "+job["error"])
        for agent in con.execute("SELECT id,status,terminal_classification,exit_code FROM dispatches "
                                 "WHERE run_id=? AND (status IN ('failed','lost','blocked') OR "
                                 "terminal_classification IN ('failed','lost','crashed','timeout'))",
                                 (run_id,)).fetchall():
            _once(con,run_id,"dispatch:"+agent["id"],"dispatch.failed",
                  "status="+str(agent["status"])+" terminal="+str(agent["terminal_classification"])+
                  " exit="+str(agent["exit_code"]))
        if rows:
            con.execute("UPDATE self_improve_runs SET cursor=?,updated_at=? WHERE run_id=?",
                        (rows[-1]["seq"],now_iso(),run_id))
        return len(rows)


def lifecycle_attempt(run_id: str | None, command: str, outcome: str, detail: str = "") -> str:
    """Called on every land/close attempt, including refused attempts; NEVER raises."""
    if os.environ.get(SELF_ENV):
        return ""
    try:
        con = db.connect()
        try:
            with db.transaction(con):
                con.execute("INSERT INTO self_improve_attempts(id,run_id,command,outcome,detail,created_at) "
                            "VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,run_id,command,outcome,sanitize(detail),now_iso()))
                if outcome == "unexpected-error" and run_id:
                    _record(con,run_id,f"{command}.error",detail,"lifecycle")
            if run_id:
                capture(con,run_id,force=True)
                result = summary(con,run_id)
                start_reporter(con)
            else:
                result = ""
        finally:
            con.close()
        return result
    except Exception:
        return "self-improve audit unavailable; retry on the next Office command"


def summary(con, run_id: str) -> str:
    rows = con.execute("SELECT status,COUNT(*) FROM self_improve_incidents WHERE run_id=? GROUP BY status",
                       (run_id,)).fetchall()
    counts = dict(rows)
    return ("self-improve: " + ", ".join(f"{counts.get(key,0)} {key}" for key in
            ("filed","pending","ready","retry","suspected")))


def _lock_path() -> Path:
    return paths.state_home() / "self-improve.lock"


def _reporter_lifetime() -> float:
    """Wall-clock bound on one detached reporter process (env-overridable)."""
    try:
        return max(1.0, float(os.environ.get("OFFICE_SELF_IMPROVE_MAX_SECONDS") or MAX_REPORTER_SECONDS))
    except ValueError:
        return MAX_REPORTER_SECONDS


def _pending_work(con) -> bool:
    row = con.execute("SELECT 1 FROM self_improve_incidents "
                      "WHERE status IN ('pending','ready','retry') LIMIT 1").fetchone()
    return row is not None


def _owner_dead() -> bool:
    """Whether the disposable-home owner that armed this state home is gone.

    OWNER_ENV unset means a persistent home (the owner is the home itself,
    bounded by the lifetime cap and the lock). Unknown is never dead: a
    failed liveness probe keeps the reporter on those bounds instead of
    reaping it on a guess."""
    raw = os.environ.get(OWNER_ENV)
    if not raw:
        return False
    try:
        pid = int(raw.rpartition("@")[0].rpartition(":")[2])
    except ValueError:
        return False
    return claim_liveness(pid, raw)[0] == DEAD


def _live(home: Path) -> bool:
    """The reporter still belongs to a living home and its living owner."""
    return home.exists() and not _owner_dead()


def _spawn_locked(lock_file) -> None:
    """Spawn `_self_improve` as the next owner of the flock `lock_file` holds.

    The locked fd is inherited (`pass_fds`), so the lock is held continuously
    from before the spawn into the child's own claim; concurrent wake-ups see
    it held and spawn nothing."""
    from office import frontdoor
    argv, extra = frontdoor.current_argv()
    env = dict(os.environ)
    env.update(extra)
    env[SELF_ENV] = "1"
    env[LOCK_FD_ENV] = str(lock_file.fileno())
    subprocess.Popen([*argv,"_self_improve"], env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True,
                     pass_fds=(lock_file.fileno(),))


def start_reporter(con=None) -> None:
    """Best-effort wake-up, bounded to one live reporter per state home.

    The per-home lock is the ownership record: it is probed non-blocking and
    held through the spawn, with the locked fd handed to the child, so
    repeated CLI wakeups (and repeated test or mutation-test invocations)
    cannot accumulate detached workers. A caller with a connection also
    skips the spawn when no incident awaits work. Either way the durable
    receipts in runs.db resume on the next Office invocation, and a reporter
    exits on its lifetime bound, its released home, or its disposable-home
    owner's death."""
    if os.environ.get(SELF_ENV):
        return
    try:
        if con is not None and not _pending_work(con):
            return
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a+") as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return  # a live reporter already owns this home
            _spawn_locked(probe)
    except Exception:
        pass


def _adopt_or_acquire():
    """The ownership lock: adopt the fd start_reporter handed over (its flock
    has been held continuously since before the spawn), else claim it here.
    None means a live reporter already owns this home."""
    lock = _lock_path()
    raw = os.environ.get(LOCK_FD_ENV)
    if raw:
        try:
            fd = int(raw)
            mine, path = os.fstat(fd), os.stat(lock)
            if (mine.st_dev, mine.st_ino) == (path.st_dev, path.st_ino):
                return os.fdopen(fd, "a+")
        except (ValueError, OSError):
            pass  # a stale or foreign fd: fall back to claiming the lock here
    f = open(lock, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def _budgeted(cap: float, budget: float | None) -> float:
    """A blocking call's timeout: its own cap, never beyond the worker's
    remaining lifetime budget."""
    try:
        remaining = float(budget)
    except (TypeError, ValueError):
        return cap
    return max(1.0, min(cap, remaining))


def _route(con):
    """Use an installed, low-cost read-only reviewer profile; never a builder."""
    from office import candidates, adapters
    rows,_ = candidates.build_candidates(con,"code_reviewer",probe=False)
    adapters_by_id = adapters.load_all()
    usable = []
    for c in rows:
        adapter = adapters_by_id.get(c["adapter_id"])
        money = (c.get("cost") or {}).get("money_estimate")
        # Only Claude currently supplies an enforceable tool-free investigator.
        # General reviewer profiles can read private state and call installed MCPs.
        if (adapter and adapter.get("id") == "claude" and money is not None and money <= 15 and
                adapters.profile(adapter,"reviewer") and (c.get("invocation_source") or "").startswith(("documented:","local-evidence:"))):
            usable.append((money,c["effort"] not in ("low","medium"),c,adapter))
    if not usable:
        raise RuntimeError("no available budget-qualified read-only investigation route")
    _,_,candidate,adapter = min(usable,key=lambda x:(x[0],x[1],x[2]["model_id"]))
    return candidate,adapter


def investigate(con, incident: dict, budget: float | None = None) -> dict:
    """Agent outputs a JSON draft, not an action. No GitHub or write tools.
    `budget` bounds the call by the worker's remaining lifetime."""
    from office import adapters
    c,adapter = _route(con)
    with tempfile.TemporaryDirectory(prefix="office-investigate-") as scratch:
        root = Path(scratch)
        output = root / "answer.json"
        isolated = {**adapter, "office_profiles": {"reviewer": {
            "argv": ["-p", "--bare", "--model", "{model}", "--effort", "{effort}",
                     "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                     "--disable-slash-commands", "--no-session-persistence", "--output-format", "text"],
            "prompt": "stdin", "output": "stdout"}}}
        argv,prof = adapters.build_argv(isolated,"reviewer",model=c["invocation_model_id"],
                                        effort=c["effort"],cwd=root,output=output)
        prompt = ("You are a cheap read-only incident investigator for Auto-Office. "
                  "No code changes, commits, PRs, network writes, or GitHub operations. "
                  "Assess ONLY this observed evidence; no broad testing. "
                  "Classify actual Auto-Office owned defects, including adapters, hooks, CLI. "
                  "External outages, user mistakes and app-specific bugs are not Auto-Office defects. "
                  "Return ONLY JSON with keys confidence (strong|weak|none), title, expected, actual, "
                  "evidence, reproduction. Never invent logs or claims. Sanitized evidence:\n"
                  + json.dumps({k:incident[k] for k in ("kind","summary","origin","occurrences")},ensure_ascii=False))
        args = argv + ([prompt] if prof.get("prompt")=="argv" else
                       [prof.get("prompt_flag","--prompt=")+prompt] if prof.get("prompt")=="argv-bound" else [])
        # Isolated cwd, no repository contents/credentials or Office run authority.
        env = {k:v for k,v in os.environ.items() if k in (
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR",
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
        proc = subprocess.run(args,input=prompt if prof.get("prompt")=="stdin" else None,
                              cwd=root,env=env,capture_output=True,text=True,timeout=_budgeted(180, budget))
        if proc.returncode:
            raise RuntimeError("investigation route failed with exit " + str(proc.returncode))
        raw = (next((p.read_text(encoding="utf-8") for p in (output,output.with_name("last-message.txt")) if p.exists()), proc.stdout)
               if prof.get("output")=="file" else proc.stdout)
        match = re.search(r"\{.*\}",raw,re.S)
        if not match:
            raise ValueError("investigator did not return structured JSON")
        result = json.loads(match.group(0))
        if result.get("confidence") not in ("strong","weak","none"):
            raise ValueError("investigator reported invalid confidence")
        for key in ("title","expected","actual","evidence","reproduction"):
            result[key] = sanitize(str(result.get(key) or ""))
        if result["confidence"]=="strong" and not all(result[k] for k in ("title","actual","evidence")):
            raise ValueError("strong report missing supporting evidence")
        return result


def _gh(*args: str, timeout: float = 90) -> str:
    p = subprocess.run(["gh",*args],text=True,capture_output=True,timeout=timeout)
    if p.returncode:
        raise RuntimeError("GitHub issue API temporarily unavailable")
    return p.stdout


def publish(incident: dict, report: dict, budget: float | None = None) -> str:
    """Issue-only external write, globally deduplicated by opaque fingerprint.
    `budget` bounds each GitHub call by the worker's remaining lifetime."""
    title = "[Auto-Office bug] " + report["title"][:95]
    fingerprint = incident["fingerprint"]
    marker = f"<!-- auto-self-improve:{fingerprint} -->"
    pages = json.loads(_gh("api", "--paginate", "--slurp",
                           f"repos/{REPO}/issues?state=all&per_page=100",
                           timeout=_budgeted(90, budget)))
    items = [item for page in pages for item in page if "pull_request" not in item]
    existing = next((it.get("html_url") for it in items if marker in (it.get("body") or "")
                     or it.get("title","").casefold()==title.casefold()),None)
    if existing:
        return existing
    body = (f"{marker}\n## Observed Auto-Office defect\n\n**Expected:** {report['expected']}\n\n"
            f"**Actual:** {report['actual']}\n\n**Observed signal:** {incident['summary']}\n\n"
            f"**Sanitized investigation:** {report['evidence']}\n\n"
            f"**Reproduction:** {report['reproduction'] or 'Not reliably reproduced'}\n\n"
            f"**Evidence type:** {incident['kind']} (observed {incident['occurrences']} time(s)).\n\n"
            "Investigation only. No fix or PR was authorized by auto-self-improve.\n")
    with tempfile.TemporaryDirectory(prefix="office-issue-") as temp:
        p = Path(temp) / "issue.md"
        p.write_text(body,encoding="utf-8")
        return _gh("issue","create","--repo",REPO,"--title",title,"--body-file",str(p),
                   timeout=_budgeted(90, budget)).strip()


def _due(con) -> list[dict]:
    rows = con.execute("SELECT * FROM self_improve_incidents WHERE status IN ('pending','ready','retry') "
                       "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY created_at LIMIT 10",
                       (now_iso(),)).fetchall()
    return [dict(r) for r in rows]


def _process(con, incident: dict, budget: float | None = None) -> None:
    fp = incident["fingerprint"]
    try:
        report = (json.loads(incident["report_json"]) if incident["report_json"]
                  else investigate(con,incident,budget))
        if report["confidence"] != "strong":
            with db.transaction(con):
                con.execute("UPDATE self_improve_incidents SET status=?,report_json=?,updated_at=? WHERE fingerprint=?",
                            ("suspected" if report["confidence"]=="weak" else "dismissed",
                             json.dumps(report),now_iso(),fp))
            return
        # First save the public report. A failed create retries without model cost.
        with db.transaction(con):
            con.execute("UPDATE self_improve_incidents SET status='ready',report_json=?,updated_at=? WHERE fingerprint=?",
                        (json.dumps(report),now_iso(),fp))
        url = publish(incident,report,budget)
        with db.transaction(con):
            con.execute("UPDATE self_improve_incidents SET status='filed',issue_url=?,last_error=NULL,updated_at=? "
                        "WHERE fingerprint=?",(url,now_iso(),fp))
    except Exception as exc:
        failures = incident["attempts"] + 1
        seconds = min(1800,30*(2**min(failures,6)))
        retry = (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()
        with db.transaction(con):
            con.execute("UPDATE self_improve_incidents SET status='retry',attempts=?,next_retry_at=?,last_error=?,"
                        "updated_at=? WHERE fingerprint=?",(failures,retry,sanitize(type(exc).__name__+": "+str(exc)),now_iso(),fp))


def _wait(delay: float, deadline: float, live) -> None:
    """Sleep toward the next retry in short slices. A released home, a dead
    disposable-home owner, or the lifetime deadline ends the wait early, so
    reclamation stays prompt."""
    remaining = max(1.0,min(delay,60.0))
    while remaining > 0 and live():
        left = deadline - time.monotonic()
        if left <= 0:
            return
        slice_seconds = min(remaining,REPORTER_POLL_SECONDS,left)
        time.sleep(slice_seconds)
        remaining -= slice_seconds


def worker(*, once: bool = False) -> int:
    """Single-owner durable retry pump with a bounded lifetime.

    The per-home lock is the ownership record, adopted as an inherited fd
    when start_reporter handed it over so ownership never gaps. The worker
    exits when nothing is pending, when its lifetime bound elapses, when its
    state home disappears, or when its disposable-home owner is gone. A
    bound elapsing with work still queued hands ownership to a fresh
    successor: retries that become due after the bound are still delivered,
    while the population stays one reporter per home and every process is
    recycled. Nothing is lost either way: pending incidents and retry
    receipts are durable in runs.db, and the next Office invocation also
    resumes them.
    """
    lock = _lock_path()
    lock.parent.mkdir(parents=True,exist_ok=True)
    f = _adopt_or_acquire()
    if f is None:
        return 0
    with f:
        deadline = time.monotonic() + _reporter_lifetime()
        con = db.connect()
        try:
            while _live(lock.parent) and time.monotonic() < deadline:
                due = _due(con)
                for incident in due:
                    if time.monotonic() >= deadline:
                        break  # the rest stay queued; the successor continues
                    _process(con,incident,deadline - time.monotonic())
                if once:
                    return 0
                if _due(con):
                    continue
                wait = con.execute("SELECT MIN(next_retry_at) FROM self_improve_incidents "
                                   "WHERE status IN ('pending','ready','retry')").fetchone()[0]
                if not wait:
                    return 0
                try:
                    delay = (datetime.fromisoformat(wait)-datetime.now(timezone.utc)).total_seconds()
                except ValueError:
                    delay = 30
                _wait(delay,deadline,lambda: _live(lock.parent))
            # Reclaimed homes take their reporter with them and spawn nothing;
            # a lifetime bound elapsing with work queued hands off to keep
            # retry delivery eventual without retaining an unbounded process.
            if _live(lock.parent) and _pending_work(con):
                try:
                    _spawn_locked(f)
                except Exception:
                    pass
            return 0
        finally:
            con.close()
