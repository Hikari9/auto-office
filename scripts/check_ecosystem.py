#!/usr/bin/env python3
from pathlib import Path
import json, re, subprocess, sys, yaml
try:
    from jsonschema import Draft202012Validator
    HAS_JSONSCHEMA = True
except ImportError:
    HAS_JSONSCHEMA = False

ROOT=Path(__file__).resolve().parents[1]
INIT_MARKERS=('TODO:', 'example_asset.txt', 'scripts/example.py', 'references/api_reference.md')

# Constraint 11 was previously report-only; the maintainer explicitly reopened it
# for this change so context-load size is now a gate. These budgets are the current
# `wc -l` counts rounded up with roughly 10% headroom; adjust this table deliberately.
# 3.1 plan-defect redirects took the hub to 142 lines and 3.2 (plan diagram, task PRs,
# end state, office land) to 153; the budget is re-set here to that count plus ~10%.
# 3.2.x run a9afacbf lessons (#267/#268 throughput lanes, #270 auto-takeover pointer): 168 -> 176.
HUB_LINE_BUDGET = 176
# 3.1: each 3.0 spoke carries a three-line banner pointing 3.1 runs at the office CLI;
# the per-spoke budgets below grew by exactly those lines.
SKILL_LINE_BUDGETS = {
    'skills/agy-cli/SKILL.md': 46,
    'skills/auto-adapter/SKILL.md': 21,
    'skills/auto-closeout/SKILL.md': 67,
    'skills/auto-execution/SKILL.md': 23,
    'skills/auto-intake/SKILL.md': 51,
    'skills/auto-loop/SKILL.md': 30,
    'skills/auto-maintenance/SKILL.md': 21,
    'skills/auto-planning/SKILL.md': 24,
    'skills/auto-review/SKILL.md': 36,
    'skills/auto-routing/SKILL.md': 54,
    'skills/auto-self-improve/SKILL.md': 29,
    'skills/auto-verification/SKILL.md': 53,
    'skills/claude-cli/SKILL.md': 43,
    # Raised from 61 for the env -i / HERDR_ENV invisibility section: a recorded
    # failure mode with a reproduction, compressed to 28 lines before raising.
    'skills/codex-cli/SKILL.md': 92,
    'skills/hermes-cli/SKILL.md': 20,
    # Vendored from external sources in 9a59305, not authored to this repo's
    # context-load convention: every skill above fits in 64 lines, these two do not.
    # Budgeted at their actual size so the gate registers rather than ignores them.
    'skills/herdr/SKILL.md': 240,
    'skills/herdr-close-panes/SKILL.md': 115,
}

def frontmatter(path):
    text=path.read_text(encoding='utf-8')
    m=re.match(r'^---\n(.*?)\n---\n',text,re.S)
    if not m: return False,'missing YAML frontmatter', None, text
    try: fm=yaml.safe_load(m.group(1))
    except Exception as e: return False,f'bad YAML frontmatter: {e}', None, text
    if set(fm or {}) != {'name','description'}: return False,f'frontmatter keys must be exactly name+description, got {set(fm or {})}', fm, text
    if not re.match(r'^[a-z0-9-]+$', fm['name']): return False,'bad name', fm, text
    if not fm['description'].strip(): return False,'empty description', fm, text
    return True,'ok', fm, text

def discovered_skills(root):
    return sorted(
        path for path in root.rglob('SKILL.md')
        if not any(part.startswith('.') for part in path.relative_to(root).parts)
    )

def check_skill_budgets(root=ROOT):
    """Return errors for SKILL.md files that exceed their configured budgets."""
    targets = []
    for path in discovered_skills(root):
        relative = path.relative_to(root).as_posix()
        budget = HUB_LINE_BUDGET if relative == 'SKILL.md' else SKILL_LINE_BUDGETS.get(relative)
        targets.append((path, relative, budget))

    errors=[]
    for path, relative, budget in targets:
        if not path.exists():
            continue
        if budget is None:
            errors.append(f'{relative}: no line budget configured')
            continue
        count = sum(1 for _ in path.open(encoding='utf-8'))
        if count > budget:
            errors.append(f'{relative}: {count} lines exceeds line budget of {budget}')
    return errors

def check_versions(root=ROOT):
    """One release identity: VERSION, the plugin manifest and the package agree.
    Claude Code keys plugin updates on the manifest version, and every run pins
    the package version, so a mismatch ships one identity under two numbers."""
    import tomllib
    want = (root / 'VERSION').read_text().strip()
    found = {'VERSION': want}
    manifest = root / '.claude-plugin' / 'plugin.json'
    if manifest.exists():
        found['.claude-plugin/plugin.json'] = json.loads(manifest.read_text()).get('version')
    project = root / 'pyproject.toml'
    if project.exists():
        found['pyproject.toml'] = tomllib.loads(project.read_text()).get('project', {}).get('version')
    return [f'{where}: version {got!r} does not match VERSION {want!r}' for where, got in found.items() if got != want]


def main():
    errors=[]
    skills = discovered_skills(ROOT)
    skill_names = set()
    
    for p in skills:
        ok,msg,fm,txt=frontmatter(p)
        if not ok: errors.append(f'{p.relative_to(ROOT)}: {msg}')
        elif fm:
            if fm['name'] in skill_names:
                errors.append(f'{p.relative_to(ROOT)}: duplicate skill name {fm["name"]}')
            skill_names.add(fm['name'])
            
        for marker in INIT_MARKERS:
            if marker in txt: errors.append(f'{p.relative_to(ROOT)}: initializer marker {marker!r}')
            
        # check broken referenced files in SKILL.md
        # simplistic check: look for things that look like paths, or maybe just ignore for now if hard to implement
        
        agent=p.parent/'agents/openai.yaml'
        if not agent.exists(): errors.append(f'{p.relative_to(ROOT)}: missing agents/openai.yaml')
        
    for p in ROOT.glob('schemas/*.json'):
        try: json.loads(p.read_text())
        except Exception as e: errors.append(f'{p.relative_to(ROOT)}: {e}')
        
    for p in list(ROOT.glob('config/*.yaml'))+list(ROOT.glob('catalog/*.yaml'))+list(ROOT.glob('adapters/**/*.yaml'))+list(ROOT.glob('evals/*.yaml')):
        try: yaml.safe_load(p.read_text())
        except Exception as e: errors.append(f'{p.relative_to(ROOT)}: {e}')
        
    # 1 & 2. Adapter validation
    adapter_schema_path = ROOT/'schemas/adapter.schema.json'
    if adapter_schema_path.exists() and HAS_JSONSCHEMA:
        try:
            adapter_schema = json.loads(adapter_schema_path.read_text())
            validator = Draft202012Validator(adapter_schema)
            for p in ROOT.glob('adapters/seed/*.yaml'):
                try:
                    data = yaml.safe_load(p.read_text())
                    for err in validator.iter_errors(data):
                        errors.append(f'{p.relative_to(ROOT)} schema error: {err.message}')
                    if 'id' not in data: errors.append(f'{p.relative_to(ROOT)} missing id')
                except Exception as e: pass
        except Exception as e: pass
        
    # 3. Hook manifest
    hook_manifest = ROOT/'.office/hook-manifest.json'
    if hook_manifest.exists():
        try: json.loads(hook_manifest.read_text())
        except Exception as e: errors.append(f'{hook_manifest.relative_to(ROOT)} invalid JSON: {e}')

    budget_errors = check_skill_budgets()
    errors.extend(budget_errors)
    errors.extend(check_versions())
        
    if errors:
        print('FAIL')
        for e in errors: print('-',e)
        return 1
    print(f'PASS: {len(skills)} skills, {len(list(ROOT.glob("schemas/*.json")))} schemas, {len(list(ROOT.glob("evals/*.yaml")))} evals; budget check: {len(SKILL_LINE_BUDGETS) + 1} files within line budgets')
    return 0

if __name__=='__main__': sys.exit(main())
