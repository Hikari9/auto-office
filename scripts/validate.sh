#!/usr/bin/env bash
# Local validation gate (replaces the removed GitHub Actions workflow; CI stays empty by
# maintainer decision, #120). Run by .githooks/pre-push; also runnable by hand.
# VALIDATE_BUILD=1 also builds the wheel and checks it carries the packaged resources.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# A git hook exports GIT_DIR, GIT_INDEX_FILE and friends. Tests that run git inside their
# own temp fixture repos would inherit them and commit onto the branch being pushed.
unset $(git rev-parse --local-env-vars)

# Same install as the old CI job: an editable package plus test deps, in a repo-local venv.
# A user-site install is not enough: some tests point HOME at a temp dir, which hides it.
# pip is configured for --user installs on some hosts, which a venv (and build's isolated env) rejects.
export PIP_USER=0
if [ ! -x .venv/bin/python3 ]; then
  echo "== Creating .venv"
  python3 -m venv .venv
  .venv/bin/python3 -m pip install -q -e '.[test]' build
fi
export PATH="$PWD/.venv/bin:$PATH"

# scripts/ is the retained 3.0 helper surface; its own tests run as that pinned helper.
export OFFICE_PINNED_LEGACY=1

echo "== Ecosystem check"
python3 scripts/check_ecosystem.py

echo "== Unit tests (3.0 helpers + 3.1 runtime; visual tests skip without Playwright)"
python3 -m pytest tests/ -q

echo "== Adapter validation"
for f in adapters/seed/*.yaml; do
  python3 scripts/office_runtime.py validate-adapter "$f"
done

echo "== Schema validation"
python3 tests/test_schemas.py

echo "== Integration test"
python3 tests/test_integration.py

if [ "${VALIDATE_BUILD:-0}" = "1" ]; then
  echo "== Build the distribution"
  rm -rf dist
  python3 -m build --wheel
  python3 -m zipfile -l dist/*.whl | grep -q office/_resources/config/config.default.yaml
fi

echo "== Validation passed"
