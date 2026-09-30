#!/usr/bin/env bash
# Local validation gate (replaces the removed GitHub Actions workflow; CI stays empty by
# maintainer decision, #120). Run by .githooks/pre-push; also runnable by hand.
# VALIDATE_BUILD=1 also builds the wheel and checks it carries the packaged resources.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# A git hook exports GIT_DIR, GIT_INDEX_FILE and friends. Tests that run git inside their
# own temp fixture repos would inherit them and commit onto the branch being pushed.
unset $(git rev-parse --local-env-vars)

# The tree under test, taken before any check runs: a commit that lands during
# the run must not be stamped as validated.
start_tree=""
[ -z "$(git status --porcelain --untracked-files=no)" ] && start_tree="$(git rev-parse 'HEAD^{tree}')"

# Same install as the old CI job: an editable package plus test deps, in a repo-local venv.
# A user-site install is not enough: some tests point HOME at a temp dir, which hides it.
# pip is configured for --user installs on some hosts, which a venv (and build's isolated env) rejects.
export PIP_USER=0
if [ ! -x .venv/bin/python3 ]; then
  echo "== Creating .venv"
  python3 -m venv .venv
fi
# Reinstall only when the dependency declaration changed, so an existing .venv picks up new
# test deps (e.g. pytest-xdist) without being rebuilt on every run.
stamp="$(shasum pyproject.toml | cut -d' ' -f1)"
if [ "$(cat .venv/.validate-stamp 2>/dev/null)" != "$stamp" ]; then
  echo "== Installing package and test deps"
  # A .venv made by `uv run`/`uv venv` has no pip; install through uv when that is the case.
  if .venv/bin/python3 -m pip --version >/dev/null 2>&1; then
    .venv/bin/python3 -m pip install -q -e '.[test]' build
  else
    uv pip install -q --python .venv/bin/python3 -e '.[test]' build
  fi
  echo "$stamp" > .venv/.validate-stamp
fi
export PATH="$PWD/.venv/bin:$PATH"

# scripts/ is the retained 3.0 helper surface; its own tests run as that pinned helper.
export OFFICE_PINNED_LEGACY=1

echo "== Ecosystem check"
python3 scripts/check_ecosystem.py

echo "== Unit tests (3.0 helpers + 3.1 runtime; visual tests skip without Playwright)"
# Two workers: the local resource budget caps test parallelism at 2 on this class of machine.
python3 -m pytest tests/ -q -n "${VALIDATE_WORKERS:-2}"

echo "== Adapter validation"
for f in adapters/seed/*.yaml; do
  python3 scripts/office_runtime.py validate-adapter "$f"
done

if [ "${VALIDATE_BUILD:-0}" = "1" ]; then
  echo "== Build the distribution"
  rm -rf dist
  python3 -m build --wheel
  python3 -m zipfile -l dist/*.whl | grep -q office/_resources/config/config.default.yaml
fi

echo "== Validation passed"
# Record the pass against the committed tree, so the pre-push hook can skip a
# rerun: a 12-minute gate inside `git push` holds the SSH connection idle and
# GitHub drops it (the push dies with SIGPIPE). Only a clean tree is stamped,
# since otherwise the tree that passed is not the one being pushed.
if [ -n "$start_tree" ] && [ -z "$(git status --porcelain --untracked-files=no)" ] \
    && [ "$(git rev-parse 'HEAD^{tree}')" = "$start_tree" ]; then
  stamps="$(git rev-parse --git-common-dir)/office-validated"
  mkdir -p "$stamps" && touch "$stamps/$start_tree"
fi
