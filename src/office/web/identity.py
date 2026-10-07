"""Stable identities for everything the web view shows.

Identities are strings with a kind prefix. They are derived from recorded
values only, so the same row always yields the same id on every host read.
The host id is the one persisted value: `ensure_host_id` creates it at the
service's explicit startup; reads use `read_host_id` or an injected value.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

HOST_ID_FILE = Path("web") / "host-id"
_GITHUB_URL = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/(?:issues|pull)/(\d+)/?$")
_NUMBER = re.compile(r"#?(\d+)/?$")


def read_host_id(state_home: Path) -> str | None:
    """The persisted host id, or None. Never creates anything."""
    try:
        raw = (Path(state_home) / HOST_ID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def ensure_host_id(state_home: Path) -> str:
    """Create the host id if absent. Only the service's startup calls this."""
    existing = read_host_id(state_home)
    if existing:
        return existing
    target = Path(state_home) / HOST_ID_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_hex(8)
    target.write_text(value + "\n", encoding="utf-8")
    return value


def host(host_id: str | None) -> str | None:
    return f"host:{host_id}" if host_id else None


def local_repo_key(git_common_dir: str | None) -> str | None:
    if not git_common_dir:
        return None
    return "repo-local:" + hashlib.sha256(str(git_common_dir).encode()).hexdigest()[:16]


def repo_key(slug: str | None, git_common_dir: str | None = None) -> str | None:
    """`repo:github.com/<owner>/<name>` when the slug is known, else the local key."""
    if slug:
        return f"repo:github.com/{slug.strip('/').lower()}"
    return local_repo_key(git_common_dir)


def run(run_id: str) -> str:
    return f"run:{run_id}"


def task(run_id: str, task_id: str) -> str:
    return f"task:{run_id}/{task_id}"


def dispatch(dispatch_id: str) -> str:
    return f"dispatch:{dispatch_id}"


def session(run_id: str, harness: str, session_id: str) -> str:
    return f"session:{run_id}/{harness}/{session_id}"


def runtime(office_version: str | None) -> str | None:
    return f"runtime:{office_version}" if office_version else None


def issue_ref(key: str | None, number: int | str) -> str | None:
    return f"issue:{key}#{number}" if key else None


def pr_ref(key: str | None, number: int | str) -> str | None:
    return f"pr:{key}#{number}" if key else None


def parse_issue(value, run_repo_key: str | None) -> dict | None:
    """landing_json.issue (a number, `#n`, or a GitHub URL) -> {ref, number, url, repo}.

    A URL names its own repository; a bare number belongs to the run's.
    """
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    m = _GITHUB_URL.search(text)
    if m:
        key = repo_key(f"{m.group(1)}/{m.group(2)}")
        number = int(m.group(3))
        url = text
    else:
        n = _NUMBER.fullmatch(text) or _NUMBER.search(text)
        if not n:
            return {"ref": None, "number": None, "url": None, "repo": run_repo_key, "raw": text}
        key, number, url = run_repo_key, int(n.group(1)), None
    return {"ref": issue_ref(key, number), "number": number, "url": url, "repo": key}
