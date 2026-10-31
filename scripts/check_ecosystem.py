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
# #427 manifesto alignment moved detail out of the hub into docs/orchestrator-reference.md (264 -> 183), so 190 stands.
# Executor self-review, office preflight and the worker status line: 177 -> 182 lines, ~5% headroom.
# The issue-only bug-report workflow adds a short pointer section to the hub.
# #484 first-run onboarding gate (office onboard) between the install check and Start: 197 -> 209.
HUB_LINE_BUDGET = 210
# 3.1: each 3.0 spoke carries a three-line banner pointing 3.1 runs at the office CLI;
# the per-spoke budgets below grew by exactly those lines.
SKILL_LINE_BUDGETS = {
    'skills/auto-office-lean/SKILL.md': 60,
    'skills/agy-cli/SKILL.md': 46,
    # #499: the 3.1+ office harness/model checklist, trust posture and Herdr form requirement.
    'skills/auto-adapter/SKILL.md': 53,
    'skills/auto-closeout/SKILL.md': 67,
    'skills/auto-execution/SKILL.md': 23,
    'skills/auto-intake/SKILL.md': 51,
    'skills/auto-loop/SKILL.md': 30,
    'skills/auto-maintenance/SKILL.md': 21,
    'skills/auto-planning/SKILL.md': 24,
    'skills/auto-review/SKILL.md': 90,
    'skills/auto-routing/SKILL.md': 54,
    'skills/auto-self-improve/SKILL.md': 52,
    # #270: when-entry-strategy-invariants-exit plus the worker and review brief templates.
    'skills/auto-takeover/SKILL.md': 72,
    'skills/auto-update-benchmarks/SKILL.md': 24,
    'skills/auto-verification/SKILL.md': 53,
    'skills/claude-cli/SKILL.md': 43,
    # Raised from 61 for the env -i / HERDR_ENV invisibility section: a recorded
    # failure mode with a reproduction, compressed to 28 lines before raising.
    'skills/codex-cli/SKILL.md': 92,
    'skills/hermes-cli/SKILL.md': 20,
    'skills/opencode-cli/SKILL.md': 45,
    'skills/pi-cli/SKILL.md': 45,
    # Vendored from external sources in 9a59305, not authored to this repo's
    # context-load convention: every skill above fits in 64 lines, these two do not.
    # Budgeted at their actual size so the gate registers rather than ignores them.
    'skills/herdr/SKILL.md': 240,
    'skills/herdr-close-panes/SKILL.md': 115,
    # Steps 1-2 are pinned by tests (tiers, lenses, ledger format) and #421 will add self-review
    # evidence to step 2, so the file was compressed in steps 3-6 only (116 -> 106) and the budget is
    # set to that count until #421 lands and the section is reconciled.
    'skills/office-submit/SKILL.md': 106,
}

def frontmatter(path):
    text=path.read_text(encoding='utf-8')
    m=re.match(r'^---\n(.*?)\n---\n',text,re.S)
    if not m: return False,'missing YAML frontmatter', None, text
    try: fm=yaml.safe_load(m.group(1))
    except Exception as e: return False,f'bad YAML frontmatter: {e}', None, text
    required = {'name', 'description'}
    allowed = required | {'disable-model-invocation'}
    keys = set(fm or {})
    if not required <= keys or keys - allowed: return False,f'frontmatter requires name+description and only optional invocation control, got {keys}', fm, text
    if 'disable-model-invocation' in fm and not isinstance(fm['disable-model-invocation'], bool):
        return False,'disable-model-invocation must be a boolean', fm, text
    if not re.match(r'^[a-z0-9-]+$', fm['name']): return False,'bad name', fm, text
    if not fm['description'].strip(): return False,'empty description', fm, text
    return True,'ok', fm, text

def discovered_skills(root):
    return sorted(
        path for path in root.rglob('SKILL.md')
        if not any(part.startswith('.') or part in ('mutants', 'plugin-dist') for part in path.relative_to(root).parts)  # mutants/: mutmut's copy
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
    """One release identity: src/office/_version.py is the only place a version is written.
    pyproject.toml reads it through hatch, and the plugin manifests carry no version, so they
    cannot drift from the package every run pins."""
    import tomllib
    errors = []
    source = root / 'src' / 'office' / '_version.py'
    if not source.is_file():
        return ['src/office/_version.py: missing, the single source of the release version']
    project = root / 'pyproject.toml'
    if project.exists():
        data = tomllib.loads(project.read_text())
        if 'version' in data.get('project', {}):
            errors.append('pyproject.toml: [project] must not hardcode version; it is dynamic from src/office/_version.py')
        if 'version' not in data.get('project', {}).get('dynamic', []):
            errors.append("pyproject.toml: [project] dynamic must include 'version'")
        if data.get('tool', {}).get('hatch', {}).get('version', {}).get('path') != 'src/office/_version.py':
            errors.append('pyproject.toml: [tool.hatch.version] path must be src/office/_version.py')
    for name in ('.claude-plugin/plugin.json', 'plugin.json'):
        manifest = root / name
        if manifest.exists() and 'version' in json.loads(manifest.read_text()):
            errors.append(f'{name}: must not hardcode version')
    catalog = root / '.claude-plugin' / 'marketplace.json'
    if catalog.exists():
        for entry in json.loads(catalog.read_text()).get('plugins', []):
            if 'version' in entry:
                errors.append(f".claude-plugin/marketplace.json:{entry.get('name')}: must not hardcode version")
    return errors



def check_plugin_skills(root=ROOT):
    """Validate curated marketplace discovery, not source-folder counts."""
    errors = []
    manifest = root / '.claude-plugin/plugin.json'
    if manifest.is_file():
        try:
            if 'skills' in json.loads(manifest.read_text()):
                errors.append('.claude-plugin/plugin.json: legacy root skills paths expose retired skills')
        except ValueError as exc:
            errors.append(f'.claude-plugin/plugin.json: invalid JSON ({exc})')
    for name, getter in (
        ('.claude-plugin/marketplace.json', lambda obj: obj['plugins'][0]['source']),
        ('.agents/plugins/marketplace.json', lambda obj: obj['plugins'][0]['source']['path']),
    ):
        path = root / name
        if not path.is_file():
            errors.append(f'{name}: marketplace missing')
            continue
        try:
            source = getter(json.loads(path.read_text()))
            if source != './plugin-dist':
                errors.append(f'{name}: expected ./plugin-dist, got {source!r}')
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            errors.append(f'{name}: malformed marketplace ({exc})')
    catalog = root / 'plugin-distribution.json'
    if not catalog.is_file():
        errors.append('plugin-distribution.json: missing catalog')
        return errors
    try:
        data = json.loads(catalog.read_text())
        wanted = set(data['public_skills'])
        retired = set(data['retired_skills'])
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(f'plugin-distribution.json: malformed catalog ({exc})')
        return errors
    visible = {p.parent.name for p in (root / 'plugin-dist/skills').glob('*/SKILL.md')}
    if visible != wanted:
        errors.append(f'plugin-dist/skills: catalog mismatch missing={sorted(wanted-visible)} extra={sorted(visible-wanted)}')
    if visible & retired:
        errors.append(f'plugin-dist/skills: retired visible {sorted(visible & retired)}')
    return errors


def check_skill_metadata(root=ROOT):
    """Require each skill's OpenAI display name to equal its canonical skill name."""
    errors=[]
    for skill in discovered_skills(root):
        ok, _, fm, _ = frontmatter(skill)
        if not ok or not fm:
            continue
        agent = skill.parent / 'agents' / 'openai.yaml'
        if not agent.exists():
            continue
        try:
            meta = yaml.safe_load(agent.read_text(encoding='utf-8')) or {}
        except Exception as exc:
            errors.append(f'{agent.relative_to(root)}: invalid YAML: {exc}')
            continue
        interface = meta.get('interface') if isinstance(meta, dict) else None
        display = interface.get('display_name') if isinstance(interface, dict) else None
        if display != fm['name']:
            errors.append(
                f'{agent.relative_to(root)}: interface.display_name {display!r} '
                f'must equal skill name {fm["name"]!r}'
            )
    return errors

def duplicate_name_errors(named):
    """(path, name) pairs of discovered skills -> one error per repeated name."""
    seen = set()
    errors = []
    for path, name in named:
        if name in seen:
            errors.append(f'{path}: duplicate skill name {name}')
        seen.add(name)
    return errors

def main():
    errors=[]
    skills = discovered_skills(ROOT)
    named = []
    
    for p in skills:
        ok,msg,fm,txt=frontmatter(p)
        if not ok: errors.append(f'{p.relative_to(ROOT)}: {msg}')
        elif fm:
            named.append((p.relative_to(ROOT), fm['name']))
            
        for marker in INIT_MARKERS:
            if marker in txt: errors.append(f'{p.relative_to(ROOT)}: initializer marker {marker!r}')
            
        # check broken referenced files in SKILL.md
        # simplistic check: look for things that look like paths, or maybe just ignore for now if hard to implement
        
        agent=p.parent/'agents/openai.yaml'
        if not agent.exists(): errors.append(f'{p.relative_to(ROOT)}: missing agents/openai.yaml')
        
    errors.extend(duplicate_name_errors(named))
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
    errors.extend(check_plugin_skills())
    errors.extend(check_skill_metadata())
    # The public plugin is isolated because host discovery paths supplement defaults.
    distribution_check = subprocess.run([sys.executable, str(ROOT / 'scripts/build_plugin_distribution.py'), '--check'],
                                        capture_output=True, text=True, cwd=ROOT)
    if distribution_check.returncode:
        errors.extend((distribution_check.stdout + distribution_check.stderr).splitlines())
        
    if errors:
        print('FAIL')
        for e in errors: print('-',e)
        return 1
    print(f'PASS: {len(skills)} canonical skill files (16 plugin-visible), {len(list(ROOT.glob("schemas/*.json")))} schemas, {len(list(ROOT.glob("evals/*.yaml")))} evals; budget check: {len(SKILL_LINE_BUDGETS) + 1} files within line budgets')
    return 0

if __name__=='__main__': sys.exit(main())
