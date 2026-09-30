"""Machine-level locations and repository identity.

runs.db is one machine-level authority. Repository config may tune policy but
may not move the authority, because two stores would mean two lifecycles.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from office.version import PACKAGE_ROOT, SOURCE_ROOT


def resources_root() -> Path:
    """Schemas, config defaults, catalog seed, adapters, and skills."""
    if (SOURCE_ROOT / "config" / "config.default.yaml").is_file():
        return SOURCE_ROOT
    return PACKAGE_ROOT / "_resources"


def _xdg(env: str, fallback: str) -> Path:
    raw = os.environ.get(env)
    return Path(raw).expanduser() if raw else Path.home() / fallback


def data_home() -> Path:
    raw = os.environ.get("OFFICE_DATA_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (_xdg("XDG_DATA_HOME", ".local/share") / "auto-office").resolve()


def state_home() -> Path:
    raw = os.environ.get("OFFICE_STATE_HOME")
    if raw:
        return Path(raw).expanduser().resolve()
    return (_xdg("XDG_STATE_HOME", ".local/state") / "auto-office").resolve()


def runs_dir() -> Path:
    return state_home() / "runs"


def run_dir(run_id: str) -> Path:
    return runs_dir() / run_id


def worktrees_dir() -> Path:
    return state_home() / "worktrees"


def runtimes_dir() -> Path:
    return data_home() / "runtimes"


def user_config_path() -> Path:
    raw = os.environ.get("OFFICE_USER_CONFIG")
    return Path(raw).expanduser() if raw else Path("~/.config/auto-office/config.yaml").expanduser()


def runs_db() -> Path:
    """AUTO_OFFICE_RUNS_DB > OFFICE_DATA_HOME > user config paths.runs_db > default."""
    override = os.environ.get("AUTO_OFFICE_RUNS_DB")
    if override:
        return Path(override).expanduser().resolve()
    if os.environ.get("OFFICE_DATA_HOME"):
        return data_home() / "runs.db"
    user = user_config_path()
    if user.is_file():
        try:
            cfg = yaml.safe_load(user.read_text(encoding="utf-8")) or {}
            raw = ((cfg.get("paths") or {}).get("runs_db"))
            if isinstance(raw, str) and raw.strip():
                return Path(raw).expanduser().resolve()
        except (OSError, yaml.YAMLError):
            pass
    return data_home() / "runs.db"


def git(cwd: Path | str, *args: str, check: bool = True, env: dict | None = None,
        input_text: str | None = None) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env=env, input=input_text)
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stderr.strip())
    return proc.stdout.strip() if proc.returncode == 0 else ""


FALLBACK_IDENTITY = ("Auto Office", "office@localhost")


def commit_identity_env(repo: Path | str) -> dict:
    """GIT_AUTHOR_*/GIT_COMMITTER_* for commits Office makes on the operator's
    behalf. The identity is OFFICE_GIT_NAME/OFFICE_GIT_EMAIL when set, else the
    repo's own `git config user.name/user.email`, so a host that verifies the
    commit author (Vercel blocks a deployment when GitHub cannot match the
    email to an account) sees the operator. The placeholder identity is used
    only when neither is configured. Provenance travels in an `Office-Run:`
    trailer on the message instead."""
    name = os.environ.get("OFFICE_GIT_NAME") or git(repo, "config", "user.name", check=False)
    email = os.environ.get("OFFICE_GIT_EMAIL") or git(repo, "config", "user.email", check=False)
    if not (name and email):
        name, email = FALLBACK_IDENTITY
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def office_trailer(run_id: str) -> str:
    return f"Office-Run: {run_id}"


class GitError(RuntimeError):
    def __init__(self, args, code, stderr):
        super().__init__(f"git {' '.join(args)} failed ({code}): {stderr}")
        self.code = code
        self.stderr = stderr


def repo_identity(cwd: Path | str | None = None) -> tuple[Path, Path] | None:
    """(worktree toplevel, git common dir) for cwd, or None outside git.

    The common dir is the same for every worktree of one repository, so it is
    the key that groups runs by repository.
    """
    cwd = Path(cwd or os.getcwd())
    try:
        top = git(cwd, "rev-parse", "--show-toplevel")
        common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except (GitError, OSError):
        return None
    if not top or not common:
        return None
    return Path(top).resolve(), Path(common).resolve()


def primary_checkout(common_dir: Path) -> Path:
    """The main worktree for a git common dir (`<repo>/.git` -> `<repo>`)."""
    return common_dir.parent if common_dir.name == ".git" else common_dir
