#!/usr/bin/env bash
# Full validation remains available; --pr runs contracts, safety smoke and affected tests.
# No GitHub Actions CI by maintainer decision (#120).
# The pre-push hook invokes --pr; bare invocation keeps the full regression gate.
# VALIDATE_BUILD=1 also builds the wheel and checks it carries the packaged resources.
set -euo pipefail
mode="full"
case "${1:-}" in
  --pr) mode="pr"; shift ;;
  --full) shift ;;
  "") ;;
  *) echo "Usage: scripts/validate.sh [--pr|--full]" >&2; exit 2 ;;
esac
if [ "$#" -ne 0 ]; then
  echo "Unexpected validate arguments: $*" >&2
  exit 2
fi
if [ "${VALIDATE_WORKERS:-2}" -gt 2 ] || [ "${VALIDATE_WORKERS:-2}" -lt 1 ]; then
  echo "[VALIDATE] VALIDATE_WORKERS must be 1 or 2" >&2
  exit 2
fi
cd "$(git rev-parse --show-toplevel)"

# A git hook exports GIT_DIR, GIT_INDEX_FILE and friends. Tests that run git inside their
# own temp fixture repos would inherit them and commit onto the branch being pushed.
unset $(git rev-parse --local-env-vars)

# Scope test evidence to exactly the pushed commit. Dirty tracked code or
# untracked runnable sources could shadow the committed tree and falsely pass.
if [ "$mode" = "pr" ]; then
  input_paths=(src tests scripts adapters catalog config .githooks ':(top,glob)*.py' pytest.ini tox.ini setup.cfg pyproject.toml)
  untracked="$(git ls-files --others --exclude-standard -- "${input_paths[@]}")"
  ignored="$(git ls-files --others --ignored --exclude-standard -- "${input_paths[@]}" | \
    grep -Ev '(^|/)(__pycache__|\.pytest_cache)/|\.py[co]$|(^|/)\.DS_Store$' || true)"
  if ! git diff --quiet HEAD -- || [ -n "$untracked" ] || [ -n "$ignored" ]; then
    echo "[PR GATE] Refusing a dirty source/test tree; commit changes before validation" >&2
    exit 2
  fi
  if [ ! -d tests/contracts ] || [ -z "$(find tests/contracts -type f -name 'test_*.py' -print -quit)" ]; then
    echo "[PR GATE] tests/contracts is missing or has no test modules; refusing scoped validation" >&2
    exit 2
  fi
fi


# The tree under test, taken before any check runs: a commit that lands during
# the run must not be stamped as validated.
start_tree=""
[ -z "$(git status --porcelain --untracked-files=no)" ] && start_tree="$(git rev-parse 'HEAD^{tree}')"

# Same install as the old CI job: an editable package plus test deps, in a repo-local venv.
# A user-site install is not enough: some tests point HOME at a temp dir, which hides it.
# pip is configured for --user installs on some hosts, which a venv (and build's isolated env) rejects.
export PIP_USER=0
if [ -x .venv/bin/python3 ] && ! .venv/bin/python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "== Recreating .venv with a supported Python"
  rm -rf .venv
fi
if [ ! -x .venv/bin/python3 ]; then
  echo "== Creating .venv"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python '>=3.11' .venv
  else
    python3 -m venv .venv
  fi
fi
if ! .venv/bin/python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "[VALIDATE] Python 3.11 or newer is required" >&2
  exit 2
fi
# Reinstall only when the dependency declaration changed, so an existing .venv picks up new
# test deps (e.g. pytest-xdist) without being rebuilt on every run.
stamp="$(shasum pyproject.toml | cut -d' ' -f1)"
if [ "$(cat .venv/.validate-stamp 2>/dev/null)" != "$stamp" ]; then
  echo "== Installing package and test deps"
  # Use uv for the Hatchling/PEP 660 editable install and host pip configuration.
  if command -v uv >/dev/null 2>&1; then
    uv pip install -q --python .venv/bin/python3 -e '.[test]' build
  else
    .venv/bin/python3 -m pip install -q -e '.[test]' build
  fi
  echo "$stamp" > .venv/.validate-stamp
fi
export PATH="$PWD/.venv/bin:$PATH"

# scripts/ is the retained 3.0 helper surface; its own tests run as that pinned helper.
export OFFICE_PINNED_LEGACY=1
# Inherited pytest configuration can silently filter collection even with
# --all (notably PYTEST_ADDOPTS='-m unit'). The gate owns its selection.
unset PYTEST_ADDOPTS PYTEST_PLUGINS

echo "== Ecosystem check"
python3 scripts/check_ecosystem.py

# Two workers maximum. PR selection still uses --all so selected integration,
# legacy, and slow tests are never hidden by the default tier filter.
if [ "$mode" = "pr" ]; then
  selection_file="$(mktemp)"
  trap 'rm -f "$selection_file"' EXIT
  python3 scripts/select_pr_tests.py --output "$selection_file"
  selected_tests=()
  while IFS= read -r selected_test; do
    selected_tests+=("$selected_test")
  done < "$selection_file"
  if [ "${#selected_tests[@]}" -eq 0 ]; then
    echo "[PR GATE] No tests selected; refusing empty scoped validation" >&2
    exit 2
  fi
  echo "== SCOPED PR tests: contract suite + safety smoke + changed-file affected tests"
  python3 -m pytest tests/contracts/ "${selected_tests[@]}" -q -n "${VALIDATE_WORKERS:-2}" --all
else
  echo "== Full test suite (unit, integration, legacy, slow; visual tests skip without Playwright)"
  python3 -m pytest tests/ -q -n "${VALIDATE_WORKERS:-2}" --all
fi

echo "== Adapter validation"
for f in adapters/seed/*.yaml; do
  python3 scripts/office_runtime.py validate-adapter "$f"
done

if [ "${VALIDATE_BUILD:-0}" = "1" ]; then
  echo "== Build the distribution"
  rm -rf dist
  python3 -m build --wheel
  python3 -m zipfile -l dist/*.whl | grep -q office/_resources/config/config.default.yaml
  python3 -m zipfile -l dist/*.whl | grep -q office/_resources/docs/orchestrator-reference.md
  python3 -m zipfile -l dist/*.whl | grep -q office/_resources/docs/onboarding.md
fi

echo "== Validation passed"
# Record the pass against the committed tree, so the pre-push hook can skip a
# rerun: a 12-minute gate inside `git push` holds the SSH connection idle and
# GitHub drops it (the push dies with SIGPIPE). Only a clean tree is stamped,
# since otherwise the tree that passed is not the one being pushed.
if [ -n "$start_tree" ] && [ -z "$(git status --porcelain --untracked-files=no)" ] \
    && [ "$(git rev-parse 'HEAD^{tree}')" = "$start_tree" ]; then
  # Keep scoped and full receipts separate; scoped results must never
  # satisfy a full-suite validation stamp.
  if [ "$mode" = "pr" ]; then
    stamps="$(git rev-parse --git-common-dir)/office-pr-validated"
  else
    stamps="$(git rev-parse --git-common-dir)/office-validated"
  fi
  mkdir -p "$stamps" && touch "$stamps/$start_tree"
fi
