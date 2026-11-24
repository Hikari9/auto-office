#!/usr/bin/env bash
set -euo pipefail
# A stock Windows install has no working `python3` in Git Bash (it is absent, or the Microsoft Store stub):
# use the first Python 3 that runs. Elsewhere this is `python3`, as before. OFFICE_PYTHON overrides it.
PYTHON3="${OFFICE_PYTHON:-}"
if [[ -z "$PYTHON3" ]]; then
  for _py in python3 python; do
    if "$_py" -c 'import sys; sys.exit(sys.version_info[0] != 3)' >/dev/null 2>&1; then PYTHON3="$_py"; break; fi
  done
fi
PYTHON3="${PYTHON3:-python3}"

OFFICE_STATE_DIR="${OFFICE_STATE_DIR:-.office}"
# Under Git Bash on Windows, `pwd -W` names the directory as C:/Users/..., which the harnesses can run from any
# shell; plain `pwd` says /c/Users/... or /tmp/..., which only an MSYS shell resolves. Elsewhere it is `pwd`.
native_pwd() { pwd -W 2>/dev/null || pwd; }
OFFICE_STATE_DIR="$(cd "$(dirname "$OFFICE_STATE_DIR")" && native_pwd)/$(basename "$OFFICE_STATE_DIR")"
HOOKS_DEST="${OFFICE_STATE_DIR}/hooks"
MANIFEST="${OFFICE_STATE_DIR}/hook-manifest.json"

UNINSTALL=0
if [ "${1:-}" = "--uninstall" ]; then
    UNINSTALL=1
fi

if [ $UNINSTALL -eq 1 ]; then
    rm -rf "$HOOKS_DEST"
    rm -f "$MANIFEST"
    echo "Uninstalled hooks."
    exit 0
fi

mkdir -p "$HOOKS_DEST"
HOOKS_SRC="$(cd "$(dirname "$0")" && pwd)"

# Resolve Node in the caller's environment. `command -v` finds ordinary PATH
# installs and nvm shims; `type -P` unwraps a shell function to its executable
# instead of invoking a potentially stale function preamble. An explicit
# OFFICE_NODE_BIN may still name a command when the caller intentionally needs
# shell-function resolution.
resolve_node() {
    local candidate
    if [ -n "${OFFICE_NODE_BIN:-}" ]; then
        printf '%s\n' "$OFFICE_NODE_BIN"
        return 0
    fi

    candidate="$(command -v node 2>/dev/null || true)"
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi

    candidate="$(type -P node 2>/dev/null || true)"
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    return 1
}

if ! NODE_BIN="$(resolve_node)"; then
    NODE_BIN=:
    echo "Node.js not found; Node-based close_finished_panes hooks will be no-ops." >&2
fi
if [ "$NODE_BIN" != ":" ] && command -v cygpath >/dev/null 2>&1; then
    NODE_BIN="$(cygpath -m "$NODE_BIN")"  # /c/Program Files/nodejs/node -> C:/Program Files/nodejs/node
fi
cp "${HOOKS_SRC}/session_end.sh" "$HOOKS_DEST/"
cp "${HOOKS_SRC}/pre_compact.sh" "$HOOKS_DEST/"
cp "${HOOKS_SRC}/compact_advisor.sh" "$HOOKS_DEST/"
cp "${HOOKS_SRC}/close_panes.sh" "$HOOKS_DEST/"
cp "${HOOKS_SRC}/close_finished_panes.mjs" "$HOOKS_DEST/"
cp "${HOOKS_SRC}/pre_tool_use.py" "$HOOKS_DEST/"
chmod +x "$HOOKS_DEST"/*.sh "$HOOKS_DEST"/*.mjs
chmod +x "$HOOKS_DEST"/*.py

cat > "$MANIFEST" << EOF
{
  "installed_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "hooks": [
    {
      "name": "session_end",
      "trigger": "session_end",
      "script": "${HOOKS_DEST}/session_end.sh",
      "idempotent": true,
      "timeout": 30
    },
    {
      "name": "pre_compact",
      "trigger": "pre_compact",
      "script": "${HOOKS_DEST}/pre_compact.sh",
      "idempotent": true,
      "timeout": 30
    },
    {
      "name": "compact_advisor",
      "trigger": "post_compact",
      "script": "${HOOKS_DEST}/compact_advisor.sh",
      "idempotent": true,
      "timeout": 30
    },
    {
      "name": "close_panes",
      "trigger": "cleanup",
      "script": "${HOOKS_DEST}/close_panes.sh",
      "idempotent": true,
      "timeout": 30
    },
    {
      "name": "close_finished_panes",
      "trigger": "stop",
      "script": "${HOOKS_DEST}/close_finished_panes.mjs",
      "idempotent": true,
      "timeout": 30
    },
    {
      "name": "pre_tool_use_approval_gate",
      "trigger": "pre_tool_use",
      "event": "PreToolUse",
      "script": "${HOOKS_DEST}/pre_tool_use.py",
      "idempotent": true,
      "timeout": 30
    }
  ]
}
EOF

"$PYTHON3" -c "
import json, os, shlex
# The HOME this script was given: Windows Python resolves ~ from USERPROFILE, so expanduser would
# configure the real user's harnesses even when the caller pointed HOME elsewhere.
home = lambda rel: os.path.join(os.environ.get('HOME') or os.path.expanduser('~'), rel)
h = '${HOOKS_DEST}'
node_bin = '${NODE_BIN}'
# Each hook is a shell command: quote its words, or a path with a space (C:/Program Files/nodejs/node,
# a home under a spaced user name) splits into two.
q = shlex.quote
configured = []

def remove_stale_telemetry_hooks(value):
    if isinstance(value, dict):
        if 'command' in value and 'catch-up.mjs' in str(value['command']):
            return None
        cleaned = {}
        for key, item in value.items():
            result = remove_stale_telemetry_hooks(item)
            if result is not None:
                cleaned[key] = result
        return cleaned
    if isinstance(value, list):
        return [result for item in value
                if (result := remove_stale_telemetry_hooks(item)) is not None]
    return value

def configure(path, updates):
    if not os.path.exists(path):
        return False
    with open(path, 'r') as f: d = remove_stale_telemetry_hooks(json.load(f))
    d.update(updates)
    with open(path, 'w') as f: json.dump(d, f, indent=2)
    return True

def configure_codex(path):
    if not os.path.exists(path):
        return False
    with open(path, 'r') as f: d = remove_stale_telemetry_hooks(json.load(f))
    hooks = d.setdefault('hooks', {})
    hooks.update({
        'SessionStart': [{'hooks': [{'type': 'command', 'command': q(h+'/session_end.sh'), 'timeout': 30000}]}],
        'PreCompact': [{'hooks': [{'type': 'command', 'command': q(h+'/pre_compact.sh'), 'timeout': 30000}]}],
        'PreToolUse': [{'hooks': [{'type': 'command', 'command': q(h+'/pre_tool_use.py'), 'timeout': 30000}]}]
    })
    with open(path, 'w') as f: json.dump(d, f, indent=2)
    return True
# Claude
p = home('.claude/settings.json')
if configure(p, {}):
    with open(p, 'r') as f: d = json.load(f)
    d.setdefault('hooks', {})
    d['hooks'].update({
        'SessionEnd': [{'hooks': [{'type': 'command', 'command': q(h+'/session_end.sh'), 'timeout': 30000}]}],
        'PreCompact': [{'hooks': [{'type': 'command', 'command': q(h+'/pre_compact.sh'), 'timeout': 30000}]}],
        'PreToolUse': [{'matcher': 'Edit|MultiEdit|Write|NotebookEdit|ApplyPatch',
                        'hooks': [{'type': 'command', 'command': q(h+'/pre_tool_use.py'), 'timeout': 30000}]}],
        'Stop': [{'hooks': [{'type': 'command', 'command': q(h+'/close_panes.sh'), 'timeout': 30000},
                            {'type': 'command', 'command': q(node_bin)+' '+q(h+'/close_finished_panes.mjs'), 'timeout': 30000}]}]
    })
    with open(p, 'w') as f: json.dump(d, f, indent=2)
    configured.append('Claude')

# Codex
p = home('.codex/hooks.json')
if configure_codex(p):
    configured.append('Codex')

# Gemini
p = home('.gemini/config/hooks.json')
if configure(p, {'SessionEnd': q(h+'/session_end.sh'),
                 'Stop': [q(h+'/close_panes.sh'), q(node_bin)+' '+q(h+'/close_finished_panes.mjs')]}):
    configured.append('Gemini')

if configured:
    print('Configured harnesses:', ', '.join(configured))
"

"$PYTHON3" -c "
import os, glob, yaml
# The HOME this script was given: Windows Python resolves ~ from USERPROFILE, so expanduser would
# configure the real user's harnesses even when the caller pointed HOME elsewhere.
home = lambda rel: os.path.join(os.environ.get('HOME') or os.path.expanduser('~'), rel)
h = '${HOOKS_DEST}'
hermes = False
for p in glob.glob(home('.hermes/profiles/*/config.yaml')):
    with open(p, 'r') as f: d = yaml.safe_load(f) or {}
    d.setdefault('hooks', {})
    d['hooks'].update({'on_session_end': h+'/session_end.sh', 'on_session_finalize': h+'/session_end.sh'})
    with open(p, 'w') as f: yaml.safe_dump(d, f)
    hermes = True
if hermes:
    print('Configured harnesses: Hermes')
" 2>/dev/null || true


echo "Hooks installed to $HOOKS_DEST and manifest created at $MANIFEST"
exit 0
