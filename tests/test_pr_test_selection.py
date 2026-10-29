"""Regression coverage for Auto-Office's targeted PR selector (#854)."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.select_pr_tests import SAFETY_SMOKE, SelectionError, selection


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def pr_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for path, text in {
        "tests/test_version_single_source.py": "def test_version(): pass\n",
        "tests/v31/test_verification_coverage.py": "def test_coverage(): pass\n",
        "tests/v31/test_issue_563_artifact_integrity.py": "def test_artifact_integrity(): pass\n",
        "tests/test_pr_test_selection.py": "def test_selector(): pass\n",
        "tests/test_routing.py": "from office import routing\n",
        "tests/v31/test_routing_fallback.py": "from office.routing import dispatch\n",
        "tests/test_unrelated.py": "from office import irrelevant\n",
        "src/office/routing.py": "def dispatch(): pass\n",
        "README.md": "baseline\n",
    }.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "add", ".")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "baseline"], check=True)
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(root, "checkout", "-qb", "feature")
    return root


def test_changed_runtime_module_selects_importers_but_not_all_tests(pr_repo):
    (pr_repo / "src/office/routing.py").write_text("def dispatch(): return 2\n")
    base, changed, chosen = selection(pr_repo)
    assert len(base) == 40
    assert changed == ["src/office/routing.py"]
    assert "tests/test_routing.py" in chosen
    assert "tests/v31/test_routing_fallback.py" in chosen
    assert "tests/test_unrelated.py" not in chosen
    assert all(p.startswith("tests/") and (pr_repo / p).is_file() for p in chosen)


def test_modified_test_module_is_selected(pr_repo):
    (pr_repo / "tests/test_unrelated.py").write_text("def test_new(): assert True\n")
    _, _, chosen = selection(pr_repo)
    assert "tests/test_unrelated.py" in chosen


def test_docs_change_still_checks_named_safety_smoke(pr_repo):
    (pr_repo / "README.md").write_text("new docs\n")
    _, changed, chosen = selection(pr_repo)
    assert changed == ["README.md"]
    assert set(SAFETY_SMOKE) <= set(chosen)


def test_version_invariant_runs_on_every_pr(pr_repo):
    (pr_repo / "README.md").write_text("new docs\n")
    _, _, chosen = selection(pr_repo)
    assert "tests/test_version_single_source.py" in chosen


def test_line_break_in_selected_test_path_fails_closed(pr_repo):
    # One name holds a newline and a pytest option; split at the newline it becomes an argument.
    (pr_repo / "tests" / "test_evil\n-k x.py").write_text("def test_evil(): pass\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "line break name"], check=True)
    with pytest.raises(SelectionError, match="line break"):
        selection(pr_repo)


def _commit_index_path(pr_repo, path: str) -> None:
    """Commit a blob under a name the working tree may not hold (non-UTF-8 is refused on APFS)."""
    blob = subprocess.run(["git", "-C", str(pr_repo), "hash-object", "-w", "--stdin"],
                          input="def test_odd(): pass\n", capture_output=True, text=True,
                          check=True).stdout.strip()
    git(pr_repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "odd name"], check=True)


def test_carriage_return_in_selected_path_is_named_not_translated(pr_repo):
    _commit_index_path(pr_repo, "tests/test_c\rx.py")
    with pytest.raises(SelectionError, match="line break") as excinfo:
        selection(pr_repo)
    # Text mode would turn \r into \n, and the message would show that instead.
    assert repr("tests/test_c\rx.py") in str(excinfo.value)


def test_non_utf8_path_fails_closed_with_its_own_message(pr_repo):
    import os

    _commit_index_path(pr_repo, os.fsdecode(b"tests/test_bad\xff.py"))
    with pytest.raises(SelectionError, match="non-UTF-8"):
        selection(pr_repo)


def test_non_ascii_test_module_is_selected_not_quoted(pr_repo):
    # Newline git output quotes this path, so it would look like a non-test change.
    (pr_repo / "tests" / "test_café.py").write_text("def test_cafe(): pass\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "non-ascii test"], check=True)
    _, changed, chosen = selection(pr_repo)
    assert changed == ["tests/test_café.py"]
    assert "tests/test_café.py" in chosen


def test_missing_safety_smoke_module_fails_closed(pr_repo):
    from scripts.select_pr_tests import SAFETY_SMOKE

    # Absent from the base as well, so the deleted-test guard does not fire first.
    git(pr_repo, "rm", "-q", SAFETY_SMOKE[0])
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "drop smoke"], check=True)
    git(pr_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (pr_repo / "README.md").write_text("new docs\n")
    with pytest.raises(SelectionError, match="Safety-smoke test modules are missing"):
        selection(pr_repo)


def test_safety_smoke_set_is_named_and_selected(pr_repo):
    from scripts.select_pr_tests import SAFETY_SMOKE

    (pr_repo / "README.md").write_text("new docs\n")
    reasons = {}
    _, _, chosen = selection(pr_repo, reasons=reasons)
    assert set(SAFETY_SMOKE) <= set(chosen)
    assert all("named safety smoke" in reasons[name] for name in SAFETY_SMOKE)


@pytest.mark.parametrize("contracts", ["missing", "empty"])
def test_pr_shell_gate_fails_closed_without_contract_tests(pr_repo, contracts):
    import os

    script = Path(__file__).resolve().parents[1] / "scripts" / "validate.sh"
    dest = pr_repo / "scripts" / "validate.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(script.read_text())
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "commit script"], check=True)
    if contracts == "empty":
        (pr_repo / "tests" / "contracts").mkdir()
    proc = subprocess.run(["bash", str(dest), "--pr"], cwd=pr_repo,
                          capture_output=True, text=True, env={**os.environ, "VALIDATE_WORKERS": "2"})
    assert proc.returncode == 2
    assert "tests/contracts is missing or has no test modules" in proc.stderr
    assert not (pr_repo / ".venv").exists()


def _install_shell_gate_fixture(pr_repo):
    import hashlib

    script = Path(__file__).resolve().parents[1] / "scripts" / "validate.sh"
    (pr_repo / "scripts").mkdir(exist_ok=True)
    (pr_repo / "scripts" / "validate.sh").write_text(script.read_text())
    for path in ("scripts/check_ecosystem.py", "scripts/select_pr_tests.py",
                 "scripts/office_runtime.py"):
        (pr_repo / path).write_text("pass\n")
    (pr_repo / "tests" / "conftest.py").write_text(
        'def pytest_addoption(parser):\n    parser.addoption("--all", action="store_true")\n')
    smoke = pr_repo / "tests" / "v31" / "test_verification_coverage.py"
    smoke.parent.mkdir(parents=True, exist_ok=True)
    smoke.write_text("def test_safety_smoke(): assert True\n")
    integrity = pr_repo / "tests" / "v31" / "test_issue_563_artifact_integrity.py"
    integrity.write_text("def test_safety_smoke(): assert True\n")
    selector_smoke = pr_repo / "tests" / "test_pr_test_selection.py"
    selector_smoke.write_text("def test_safety_smoke(): assert True\n")
    (pr_repo / "tests" / "test_affected.py").write_text("def test_affected(): assert True\n")
    contracts = pr_repo / "tests" / "contracts"
    contracts.mkdir()
    (contracts / "test_contract.py").write_text("def test_contract(): assert True\n")
    (pr_repo / "pyproject.toml").write_text("[project]\nname = 'gate-fixture'\n")
    (pr_repo / ".gitignore").write_text(".venv/\n")
    env_dir = pr_repo / ".venv" / "bin"
    env_dir.mkdir(parents=True)
    real_python = shlex.quote(sys.executable)
    fake_python = f'''#!/bin/bash
if [ "$1" = "-c" ]; then
  case "$2" in *hashlib*) exec {real_python} "$@" ;; esac
  [ -z "$PY_TOO_OLD" ]; exit $?
fi
case "$1" in
  scripts/check_ecosystem.py) exit 0 ;;
  scripts/select_pr_tests.py)
    if [ -n "$SELECTED_TESTS" ]; then printf '%s\\n' "$SELECTED_TESTS" > "$3"; else : > "$3"; fi
    exit 0 ;;
  scripts/office_runtime.py) exit 0 ;;
  -m)
    if [ -n "$PYTEST_ARGS_LOG" ]; then printf '%s\\n' "$@" > "$PYTEST_ARGS_LOG"; fi
    exec {real_python} "$@" ;;
esac
exit 99
'''
    (env_dir / "python3").write_text(fake_python)
    (env_dir / "python3").chmod(0o755)
    stamp = hashlib.sha256((pr_repo / "pyproject.toml").read_bytes()).hexdigest()
    (pr_repo / ".venv" / ".validate-stamp").write_text(stamp + "\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "gate fixture"], check=True)
    return pr_repo / "scripts" / "validate.sh"


def test_pr_shell_gate_executes_contracts_and_affected_selection(pr_repo):
    import os

    script = _install_shell_gate_fixture(pr_repo)
    selected_tests = "\n".join((*SAFETY_SMOKE, "tests/test_affected.py"))
    args_log = pr_repo / "pytest-args.log"
    proc = subprocess.run(["/bin/bash", str(script), "--pr"], cwd=pr_repo,
                          capture_output=True, text=True,
                          env={**os.environ, "VALIDATE_WORKERS": "1",
                               "SELECTED_TESTS": selected_tests,
                               "PYTEST_ARGS_LOG": str(args_log)})
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "SCOPED PR tests: contract suite" in proc.stdout
    assert "contract suite + safety smoke + changed-file affected tests" in proc.stdout
    assert re.search(r"\b6 passed\b", proc.stdout)
    pytest_args = args_log.read_text().splitlines()
    assert "tests/contracts/" in pytest_args
    assert (set(SAFETY_SMOKE) | {"tests/test_affected.py"}) <= set(pytest_args)


def test_pr_shell_gate_handles_empty_selection_on_system_bash(pr_repo):
    import os

    script = _install_shell_gate_fixture(pr_repo)
    proc = subprocess.run(["/bin/bash", str(script), "--pr"], cwd=pr_repo,
                          capture_output=True, text=True,
                          env={**os.environ, "VALIDATE_WORKERS": "1"})
    assert proc.returncode == 2
    assert "No tests selected; refusing empty scoped validation" in proc.stderr


@pytest.mark.parametrize("too_old", [False, True])
def test_pr_shell_gate_requests_any_supported_python_from_uv(pr_repo, too_old):
    import os
    import shutil

    script = _install_shell_gate_fixture(pr_repo)
    selected_tests = "\n".join((*SAFETY_SMOKE, "tests/test_affected.py"))
    python_stub = pr_repo / "python3.stub"
    shutil.copy2(pr_repo / ".venv" / "bin" / "python3", python_stub)
    if not too_old:
        # A too-old existing .venv is recreated instead; keep it for that case.
        shutil.rmtree(pr_repo / ".venv")
    fake_bin = pr_repo / "fake-bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$UV_LOG"
if [ "$1" = "venv" ]; then
  mkdir -p .venv/bin
  cp "$PYTHON_STUB" .venv/bin/python3
  chmod +x .venv/bin/python3
  exit 0
fi
if [ "$1" = "pip" ]; then exit 0; fi
exit 99
''')
    uv.chmod(0o755)
    env_log = pr_repo / "uv.log"
    proc = subprocess.run(["/bin/bash", str(script), "--pr"], cwd=pr_repo,
                          capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                               "PYTHON_STUB": str(python_stub), "UV_LOG": str(env_log),
                               "SELECTED_TESTS": selected_tests,
                               "PY_TOO_OLD": "1" if too_old else "",
                               "VALIDATE_WORKERS": "1"})
    assert "venv --python >=3.11 .venv" in env_log.read_text()
    if too_old:
        # The stub stays too old after recreation, so the gate must refuse rather than run.
        assert proc.returncode == 2, proc.stderr + proc.stdout
        assert "Recreating .venv with a supported Python" in proc.stdout
        assert "Python 3.11 or newer is required" in proc.stderr
        assert "SCOPED PR tests" not in proc.stdout
    else:
        assert proc.returncode == 0, proc.stderr + proc.stdout


def test_build_resource_check_survives_early_grep_match(tmp_path):
    """grep -q exiting on the first match must not SIGPIPE the listing under pipefail."""
    import os

    lines = (Path(__file__).resolve().parents[1] / "scripts" / "validate.sh").read_text().splitlines()
    start = lines.index('if [ "${VALIDATE_BUILD:-0}" = "1" ]; then')
    body = "\n".join(lines[start + 1:lines.index("fi", start)])
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    listing = tmp_path / "listing.py"
    listing.write_text(
        "for name in ('office/_resources/config/config.default.yaml',\n"
        "             'office/_resources/docs/orchestrator-reference.md',\n"
        "             'office/_resources/docs/onboarding.md'):\n"
        "    print(name)\n"
        "for i in range(200000):\n"
        "    print('filler', i, 'x' * 40)\n")
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        "#!/bin/bash\n"
        'if [ "$1" = "-m" ] && [ "$2" = "build" ]; then mkdir -p dist && : > dist/x.whl; exit 0; fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "zipfile" ]; then exec '
        f"{shlex.quote(sys.executable)} {shlex.quote(str(listing))}; fi\n"
        "exit 99\n")
    fake_python.chmod(0o755)
    proc = subprocess.run(["bash", "-c", f"set -euo pipefail\n{body}"], cwd=tmp_path,
                          capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr


def test_missing_base_fails_closed(pr_repo):
    (pr_repo / "README.md").write_text("new docs\n")
    with pytest.raises(SelectionError, match="rev-parse"):
        selection(pr_repo, "refs/remotes/origin/not-real")


def test_no_changes_cannot_attest_scoped_gate(pr_repo):
    with pytest.raises(SelectionError, match="No changed files"):
        selection(pr_repo)


def test_deleted_test_refuses_to_silently_lose_coverage(pr_repo):
    (pr_repo / "tests/test_unrelated.py").unlink()
    with pytest.raises(SelectionError, match="test file was deleted"):
        selection(pr_repo)


@pytest.mark.parametrize("destination", ["docs/test_unrelated.py", "tests/helper.py"])
def test_renamed_test_refuses_to_silently_lose_coverage(pr_repo, destination):
    (pr_repo / destination).parent.mkdir(parents=True, exist_ok=True)
    git(pr_repo, "mv", "tests/test_unrelated.py", destination)
    with pytest.raises(SelectionError, match="test file was deleted"):
        selection(pr_repo)


def test_test_infrastructure_requires_full_validation(pr_repo):
    (pr_repo / "tests/conftest.py").write_text("# changed fixtures\n")
    git(pr_repo, "add", "tests/conftest.py")
    with pytest.raises(SelectionError, match="Test infrastructure changed"):
        selection(pr_repo)


def test_pytest_configuration_requires_full_validation(pr_repo):
    (pr_repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-k smoke'\n")
    git(pr_repo, "add", "pyproject.toml")
    with pytest.raises(SelectionError, match="Test configuration changed"):
        selection(pr_repo)


def test_unmapped_script_fails_closed(pr_repo):
    path = pr_repo / "scripts/new_runtime.py"
    path.parent.mkdir(exist_ok=True)
    path.write_text("pass\n")
    git(pr_repo, "add", "scripts/new_runtime.py")
    with pytest.raises(SelectionError, match="No tests mapped"):
        selection(pr_repo)


def test_ignored_test_cannot_replace_committed_coverage(pr_repo):
    (pr_repo / ".gitignore").write_text("tests/test_new_runtime.py\n")
    (pr_repo / "tests/test_importer.py").write_text("from office import new_runtime\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "tracked importer"], check=True)
    git(pr_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    (pr_repo / "tests/test_new_runtime.py").write_text("def test_fake(): pass\n")
    (pr_repo / "src/office/new_runtime.py").write_text("pass\n")
    git(pr_repo, "add", "src/office/new_runtime.py")
    _, _, chosen = selection(pr_repo)
    assert "tests/test_new_runtime.py" not in chosen
    assert "tests/test_importer.py" in chosen


def test_many_indirect_importers_are_bounded(pr_repo):
    (pr_repo / "src/office/routing.py").write_text("def dispatch(): return 2\n")
    for idx in range(30):
        p = pr_repo / f"tests/test_extra_{idx:02d}.py"
        p.write_text("from office import routing\n")
    _, _, chosen = selection(pr_repo)
    extras = [p for p in chosen if p.startswith("tests/test_extra_")]
    assert len(extras) <= 1
    assert "tests/test_routing.py" in chosen
    assert "tests/v31/test_routing_fallback.py" in chosen


def test_uses_merge_base_so_updated_main_does_not_add_unrelated_tests(pr_repo):
    (pr_repo / "src/office/routing.py").write_text("def dispatch(): return 3\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "feature"], check=True)
    # An unrelated commit on main should not become part of the feature diff.
    git(pr_repo, "checkout", "main")
    (pr_repo / "README.md").write_text("main advanced\n")
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", "commit", "-qm", "main"], check=True)
    git(pr_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(pr_repo, "checkout", "feature")
    _, changed, _ = selection(pr_repo)
    assert changed == ["src/office/routing.py"]


def test_unmapped_runtime_file_fails_closed(pr_repo):
    path = pr_repo / "src/office/new_runtime_subsystem.py"
    path.write_text("def subtle_bug(): return False\n")
    git(pr_repo, "add", "src/office/new_runtime_subsystem.py")
    with pytest.raises(SelectionError, match="No tests mapped to changed runtime module"):
        selection(pr_repo)


@pytest.mark.parametrize("kind", ["tracked", "untracked_source", "root_conftest", "ignored_test"])
def test_pr_shell_gate_rejects_uncommitted_inputs_before_install(pr_repo, kind):
    import os
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate.sh"
    dest = pr_repo / "scripts" / "validate.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(script.read_text())
    git(pr_repo, "add", ".")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "commit script"], check=True)
    if kind == "tracked":
        (pr_repo / "README.md").write_text("dirty\n")
    elif kind == "untracked_source":
        (pr_repo / "src/office/untracked.py").write_text("SECRET = True\n")
    elif kind == "root_conftest":
        (pr_repo / "conftest.py").write_text("def pytest_collection_modifyitems(items): items.clear()\n")
    else:
        git(pr_repo, "config", "core.excludesFile", str(pr_repo / "exclude"))
        (pr_repo / "exclude").write_text("tests/test_ignored.py\n")
        (pr_repo / "tests/test_ignored.py").write_text("def test_fake(): pass\n")
    proc = subprocess.run(["bash", str(dest), "--pr"], cwd=pr_repo,
                          capture_output=True, text=True, env={**os.environ, "VALIDATE_WORKERS": "2"})
    assert proc.returncode == 2
    assert "Refusing a dirty source/test tree" in proc.stderr
    assert not (pr_repo / ".venv").exists()


def test_legacy_integration_mapping_runs_only_relevant_node(pr_repo):
    target = pr_repo / "src/office/integration.py"
    target.write_text("def compose(): return True\n")
    node = pr_repo / "tests/test_integration.py"
    node.write_text("def test_issue_577_failed_integration_check_evidence_includes_log(): pass\n")
    git(pr_repo, "add", "tests/test_integration.py")
    subprocess.run(["git", "-C", str(pr_repo), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "baseline test"], check=True)
    git(pr_repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(pr_repo, "add", "src/office/integration.py")
    _, _, chosen = selection(pr_repo)
    assert "tests/test_integration.py" not in chosen
    assert "tests/test_integration.py::test_issue_577_failed_integration_check_evidence_includes_log" in chosen
