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
from office.util import now_iso

REPO = "Hikari9/auto-office"
SELF_ENV = "OFFICE_SELF_IMPROVE_REPORTER"
# Routine gates, user mistakes, quota limits, and external outages are not bugs.
IGNORED = ("not-ready", "close-blocked", "user-quote-required", "invalid-override", "no-task",
           "usage", "quota-probe", "user-", "plan.awaiting", "self_improve", "self-improve")
SIGNAL = re.compile(r"failed|error|refused|retry|dead|orphan|lost|blocked|stalled|unavailable|invalid|mismatch|regression", re.I)
REDACTIONS = (
    (re.compile(r"(?i)(?:gh[pousr]_|sk-[a-z0-9_-]{10,}|Bearer\s+)[A-Za-z0-9_./+=-]+"), "[redacted-secret]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"https?://\S+"), "[redacted-url]"),
    (re.compile(r"(?<!\w)(?:/Users/|/home/|/tmp/|/private/|[A-Z]:\\\\)[^\s,:;]+"), "[redacted-path]"),
    (re.compile(r"(?i)\b(?:token|password|secret|apikey|api_key|authorization)\s*[:=]\s*\S+"), "[redacted-field]"),
    (re.compile(r"\b[0-9a-f]{32,}\b", re.I), "[redacted-identifier]"),
)


def sanitize(value: str) -> str:
    """Deterministic, conservative public capsule; NEVER publish unfiltered raw logs."""
    text = str(value)[:1800].replace("\x00", " ")
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
                "status=CASE WHEN status='suspected' THEN 'pending' ELSE status END",
                (key, run_id,kind,text,origin,now_iso(),now_iso()))


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
        if rows:
            con.execute("UPDATE self_improve_runs SET cursor=?,updated_at=? WHERE run_id=?",
                        (rows[-1]["seq"],now_iso(),run_id))
        return len(rows)


def lifecycle_attempt(run_id: str | None, command: str, outcome: str, detail: str = "") -> str:
    """Called on every land/close attempt, including refused attempts; NEVER raises."""
    if os.environ.get(SELF_ENV) or not run_id:
        return ""
    try:
        con = db.connect()
        try:
            with db.transaction(con):
                con.execute("INSERT INTO self_improve_attempts(id,run_id,command,outcome,detail,created_at) "
                            "VALUES(?,?,?,?,?,?)",(uuid.uuid4().hex,run_id,command,outcome,sanitize(detail),now_iso()))
                if outcome == "unexpected-error":
                    _record(con,run_id,f"{command}.error",detail,"lifecycle")
            capture(con,run_id,force=True)
            result = summary(con,run_id)
        finally:
            con.close()
        start_reporter()
        return result
    except Exception:
        return "self-improve audit unavailable; retry on the next Office command"


def summary(con, run_id: str) -> str:
    rows = con.execute("SELECT status,COUNT(*) FROM self_improve_incidents WHERE run_id=? GROUP BY status",
                       (run_id,)).fetchall()
    counts = dict(rows)
    return ("self-improve: " + ", ".join(f"{counts.get(key,0)} {key}" for key in
            ("filed","pending","ready","retry","suspected")))


def start_reporter() -> None:
    """Best effort wake-up. Worker also resumes on the next Office invocation."""
    if os.environ.get(SELF_ENV):
        return
    from office import frontdoor
    argv, extra = frontdoor.current_argv()
    env = dict(os.environ)
    env.update(extra)
    env[SELF_ENV] = "1"
    try:
        subprocess.Popen([*argv,"_self_improve"], env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError:
        pass


def _route(con):
    """Use an installed, low-cost read-only reviewer profile; never a builder."""
    from office import candidates, adapters
    rows,_ = candidates.build_candidates(con,"code_reviewer",probe=False)
    adapters_by_id = adapters.load_all()
    usable = []
    for c in rows:
        adapter = adapters_by_id.get(c["adapter_id"])
        money = (c.get("cost") or {}).get("money_estimate")
        if (adapter and money is not None and money <= 15 and
                adapters.profile(adapter,"reviewer") and (c.get("invocation_source") or "").startswith(("documented:","local-evidence:"))):
            usable.append((money,c["effort"] not in ("low","medium"),c,adapter))
    if not usable:
        raise RuntimeError("no available budget-qualified read-only investigation route")
    _,_,candidate,adapter = min(usable,key=lambda x:(x[0],x[1],x[2]["model_id"]))
    return candidate,adapter


def investigate(con, incident: dict) -> dict:
    """Agent outputs a JSON draft, not an action. No GitHub or write tools."""
    from office import adapters
    c,adapter = _route(con)
    with tempfile.TemporaryDirectory(prefix="office-investigate-") as scratch:
        root = Path(scratch)
        output = root / "answer.json"
        argv,prof = adapters.build_argv(adapter,"reviewer",model=c["invocation_model_id"],
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
        env = {k:v for k,v in os.environ.items() if k not in ("GH_TOKEN","GITHUB_TOKEN","OFFICE_RUN_ID",
               "OFFICE_TASK_ID","OFFICE_DISPATCH_ID","OFFICE_ROLE","OFFICE_STATE_DIR")}
        proc = subprocess.run(args,input=prompt if prof.get("prompt")=="stdin" else None,
                              cwd=root,env=env,capture_output=True,text=True,timeout=180)
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


def _gh(*args: str) -> str:
    p = subprocess.run(["gh",*args],text=True,capture_output=True,timeout=90)
    if p.returncode:
        raise RuntimeError("GitHub issue API temporarily unavailable")
    return p.stdout


def publish(incident: dict, report: dict) -> str:
    """Issue-only external write, globally deduplicated by opaque fingerprint."""
    title = "[Auto-Office bug] " + report["title"][:95]
    fingerprint = incident["fingerprint"]
    marker = f"<!-- auto-self-improve:{fingerprint} -->"
    items = json.loads(_gh("issue","list","--repo",REPO,"--state","all",
                           "--limit","1000","--json","title,body,url"))
    existing = next((it.get("url") for it in items if marker in (it.get("body") or "")
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
        return _gh("issue","create","--repo",REPO,"--title",title,"--body-file",str(p)).strip()


def _due(con) -> list[dict]:
    rows = con.execute("SELECT * FROM self_improve_incidents WHERE status IN ('pending','ready','retry') "
                       "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY created_at LIMIT 10",
                       (now_iso(),)).fetchall()
    return [dict(r) for r in rows]


def _process(con, incident: dict) -> None:
    fp = incident["fingerprint"]
    try:
        report = json.loads(incident["report_json"]) if incident["report_json"] else investigate(con,incident)
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
        url = publish(incident,report)
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


def worker(*, once: bool = False) -> int:
    """Single-owner durable retry pump. A future CLI call recovers after a crash."""
    lock = paths.state_home() / "self-improve.lock"
    lock.parent.mkdir(parents=True,exist_ok=True)
    with open(lock,"a+") as f:
        try:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        con = db.connect()
        try:
            while True:
                due = _due(con)
                for incident in due:
                    _process(con,incident)
                if once:
                    return 0
                wait = con.execute("SELECT MIN(next_retry_at) FROM self_improve_incidents "
                                   "WHERE status IN ('pending','ready','retry')").fetchone()[0]
                if not wait:
                    return 0
                try:
                    delay = (datetime.fromisoformat(wait)-datetime.now(timezone.utc)).total_seconds()
                except ValueError:
                    delay = 30
                time.sleep(max(1,min(delay,60)))
        finally:
            con.close()
