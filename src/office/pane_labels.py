"""Human-readable Herdr pane labels."""
from __future__ import annotations
import re
from pathlib import Path

def repo_short(run: dict) -> str:
    """The pinned override wins; otherwise shorten the repository directory."""
    cfg = (run.get("policy") or {}).get("herdr") or {}
    override = cfg.get("repo_short") if isinstance(cfg, dict) else None
    name = override.strip() if isinstance(override, str) and override.strip() else None
    if name is None:
        name = Path(str(run.get("repo_root") or "")).name
        name = re.sub(r"\.git$", "", name, flags=re.I)
        name = re.sub(r"\.(?:favor\.church|[\w-]+\.(?:com|org|net|io|dev|app)|com|org|net|io|dev|app)$",
                      "", name, flags=re.I)
    name = "".join(c for c in name if c.isprintable())
    return re.sub(r"\s+", "-", name).strip()[:32]


def short_title(value: object) -> str:
    clean = "".join(c if c.isprintable() else " " for c in str(value or ""))
    clean = re.sub(r"(?<!\S)#\d+\b", "", clean)
    return " ".join(clean.split()[:3])[:48]


def issue_number(value: object) -> str:
    raw = str(value or '').strip()
    if raw.isdigit():
        return raw
    match = re.search(r'(?:/issues/|#)([0-9]+)', raw)
    return match.group(1) if match else ''

def model_tag(harness, model, effort) -> str:
    raw = str(model or '').lower().split('/')[-1]
    alias = raw
    match = re.match(r'gpt-([0-9]+(?:[.-][0-9]+)?)-(sol|luna|astra)', raw)
    if match:
        alias = ('sol ' + match.group(1).replace('-', '.')) if match.group(2) == 'sol' else match.group(2)
    elif raw.startswith('claude-'):
        alias = raw.split('-')[1]
    elif raw.startswith('mimo-'):
        alias = 'mimo-' + raw.split('-')[-1]

    elif raw.startswith('gemini-'):
        alias = 'gemini-' + raw.split('-')[-1]
    alias = alias or str(harness or '').lower()
    level = {'none': '', 'minimal': 'min', 'medium': 'med'}.get(str(effort or ''), str(effort or ''))
    return '[' + alias + (' ' + level if level else '') + ']' if alias else ''


def role_label(role) -> str:
    if role in ('executor', 'worker', 'planner'):
        return ''
    if role in ('code_reviewer', 'plan_reviewer', 'integration_reviewer'):
        return 'review'
    if role == 'visual_reviewer':
        return 'visual review'
    if role == 'closeout_verifier':
        return 'verify'
    return str(role or '').replace('_', ' ')


def shell_label(run, dispatch) -> str:
    return ' '.join(p for p in (repo_short(run), dispatch.get('task_id') or 'plan', 'shell') if p)


def format_label(run, dispatch, scope, title, pr) -> str:
    match = re.search(r'(?<!\S)#([0-9]+)', title or '')
    issue = match.group(1) if match else issue_number((run.get('landing') or {}).get('issue'))
    reference = '#' + issue if issue else 'PR#' + str(pr) if pr else ''
    role = dispatch.get('role') or dispatch.get('kind')
    return ' '.join(p for p in (repo_short(run), scope, reference, short_title(title),
                                role_label(role), model_tag(dispatch.get('harness'), dispatch.get('model'),
                                                            dispatch.get('effort'))) if p)
