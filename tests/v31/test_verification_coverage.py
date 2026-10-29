"""#563: a partial pytest tier cannot silently certify integration for landing."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from office import land, verification_coverage as vc
from office.state import Refused


@pytest.fixture
def tiered_repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests" / "conftest.py").write_text('''
def pytest_addoption(parser):
    parser.addoption("--all", action="store_true")
def pytest_collection_modifyitems(config, items):
    # integration, legacy and slow are deselected unless --all
    if config.getoption("--all"):
        return
''')
    (root / "scripts" / "validate.sh").write_text('''#!/bin/bash
python3 -m pytest tests/ -q -n "${VALIDATE_WORKERS:-2}" --all
''')
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "tier config")
    return root, git("rev-parse", "HEAD")


def write_scoped_gate_artifacts(root: Path) -> None:
    """Materialize every digest-pinned input in a small committed fixture."""
    for path in vc._SCOPED_GATE_SHA256:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("# reviewed artifact\n")


@pytest.mark.parametrize("commands", [
    [],
    ["python3 -m pytest tests/ -q"],
    ["pytest -q --all tests/v31/test_land.py"],
    ["pytest -q --all -k smoke"],
    ["pytest -q --all -m integration"],
    ["pytest -q --all --ignore tests/v31"],
    ["echo pytest tests/ --all"],
    ["echo scripts/validate.sh"],
    ["cat scripts/validate.sh"],
    ["pytest tests/ --all -k=smoke"],
    ["pytest --all || true"],
    ["pytest --all; exit 0"],
    ["python3 -m pytest tests/ --all -c other.ini"],
    ["pytest --all -o addopts=-m=unit"],
    ["pytest --all -kfoo"],
    ["pytest --all --noconftest"],
    ["pytest --all $(echo tests/unit)"],
    ["python3 -m pytest --all --override-ini addopts='-m not slow'"],
    ["PYTEST_ADDOPTS='-m unit' python3 -m pytest --all"],
    ["env PYTEST_ADDOPTS='-m unit' pytest --all"],
])
def test_partial_coverage_is_reported_not_credited_as_complete(tiered_repo, commands):
    root, sha = tiered_repo
    grade, detail = vc.audit(root, sha, commands)
    assert grade == vc.PARTIAL and "deselected" in detail
    with pytest.raises(Refused, match="integration checks are insufficient") as refused:
        land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": commands}}, sha)
    assert refused.value.category == "integration-coverage-incomplete"


@pytest.mark.parametrize("commands", [
    ["pytest tests/ --all -q"],
    ["python3 -m pytest --all"],
    ["uv run pytest tests/ --all"],
    [".venv/bin/python -m pytest tests/ -q -n 2 --all"],
    ["env CI=1 python3 -m pytest --all"],
])
def test_full_suite_commands_are_recognized_and_landable(tiered_repo, commands):
    root, sha = tiered_repo
    assert vc.audit(root, sha, commands)[0] == vc.FULL
    land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": commands}}, sha)


def test_unfixed_validation_script_is_partial_even_when_named(tiered_repo):
    root, _ = tiered_repo
    script = root / "scripts" / "validate.sh"
    script.write_text("python3 -m pytest tests/ -q\n")
    subprocess.run(["git", "-C", str(root), "add", "scripts/validate.sh"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "incomplete gate"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, sha, ["scripts/validate.sh"])[0] == vc.PARTIAL


def test_pr_script_scope_is_not_full_suite_and_does_not_enqueue_full_duplicate(tiered_repo, monkeypatch):
    root, _ = tiered_repo
    script = root / "scripts" / "validate.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'python3 scripts/select_pr_tests.py --output "$selection_file"\n'
        'python3 -m pytest "${selected_tests[@]}" -q -n 2 --all\n'
    )
    (root / "scripts" / "select_pr_tests.py").write_text("print('select tests')\n")
    import hashlib
    write_scoped_gate_artifacts(root)
    monkeypatch.setattr(vc, "_SCOPED_GATE_SHA256", {
        p: hashlib.sha256((root / p).read_bytes()).hexdigest()
        for p in vc._SCOPED_GATE_SHA256
    })
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "scoped gate"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    command = "scripts/validate.sh --pr"
    assert vc.audit(root, sha, [command])[0] == vc.SCOPED
    assert vc.supplemental_checks(root, sha, [command]) == []
    assert vc._complete_pytest_command(command) is False
    land._require_run_coverage({"repo_root": str(root),
                                "landing": {"run_checks": [command]}}, sha)

    # A similarly named but non-functional shell command cannot claim a scope.
    script.write_text("#!/bin/sh\nexit 0\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "disable gate"], check=True)
    unsafe = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, unsafe, [command])[0] == vc.PARTIAL


def test_unverified_scoped_command_cannot_attest_full_repo():
    root = Path(__file__).resolve().parents[2]
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    # A command declaration without a valid check-output receipt cannot be
    # upgraded to a completed integration check.
    run = {"id": "r", "repo_root": str(root), "landing": {"integration": {
        "full_suite": {"commit": sha, "gate": "unproven", "command": "scripts/validate.sh --pr"}}}}
    assert vc.attested_checks(None, run, sha) == []


def test_scoped_noop_script_does_not_claim_coverage(tiered_repo):
    root, _ = tiered_repo
    (root / "scripts" / "select_pr_tests.py").write_text("print('selector')\n")
    (root / "scripts" / "validate.sh").write_text(
        "#!/usr/bin/env bash\ntrue\n"
        '# python3 scripts/select_pr_tests.py --output "$selection_file"\n'
        '# python3 -m pytest "${selected_tests[@]}" -q -n 2 --all\n')
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c",
                    "user.email=t@t", "commit", "-qm", "no-op gate"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, sha, ["scripts/validate.sh --pr"])[0] == vc.PARTIAL


def test_scoped_gate_pins_match_reviewed_source():
    import hashlib
    root = Path(__file__).resolve().parents[2]
    assert set(vc._SCOPED_GATE_SHA256) == {
        "scripts/validate.sh",
        "scripts/select_pr_tests.py",
        "tests/test_pr_test_selection.py",
        "tests/v31/test_verification_coverage.py",
        "tests/v31/test_issue_563_artifact_integrity.py",
        ".githooks/pre-push",
        "tests/contracts/test_executor_policy.py",
        "tests/contracts/test_route_parity.py",
    }
    assert {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
            for p in vc._SCOPED_GATE_SHA256} == vc._SCOPED_GATE_SHA256


@pytest.mark.parametrize("changed", list(vc._SCOPED_GATE_SHA256))
def test_scoped_gate_rejects_changed_blob(tiered_repo, changed, monkeypatch):
    import hashlib
    root, _ = tiered_repo
    for path in vc._SCOPED_GATE_SHA256:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("# reviewed artifact\n")
    monkeypatch.setattr(vc, "_SCOPED_GATE_SHA256", {
        p: hashlib.sha256((root / p).read_bytes()).hexdigest()
        for p in vc._SCOPED_GATE_SHA256
    })
    (root / changed).write_bytes((root / changed).read_bytes() + b"# drift\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "gate drift"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD",], text=True).strip()
    assert vc.audit(root, sha, ["scripts/validate.sh --pr"])[0] == vc.PARTIAL


def test_non_tiered_repo_preserves_declared_check_policy(tmp_path):
    root = tmp_path / "untiered"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "README.md").write_text("none\n")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, sha, ["pytest -q"]) is None
    land._require_run_coverage({"repo_root": str(root), "landing": {"run_checks": ["pytest -q"]}}, sha)


def test_repositories_own_validation_script_runs_all_tiers():
    script = (Path(__file__).resolve().parents[2] / "scripts" / "validate.sh").read_text()
    assert any(vc._complete_pytest_command(line) for line in script.splitlines()
               if line.strip().startswith("python3 -m pytest")), "validate.sh must run --all without tier filters"
    assert "unset PYTEST_ADDOPTS PYTEST_PLUGINS" in script


def test_land_cli_refuses_incomplete_tiered_integration_before_any_merge(env, monkeypatch):
    """End-to-end: PASS on an empty/default tier must not merge a real task PR."""
    import json
    from test_land import _plan, _run
    from test_task_prs import gh, remote_head

    (env.repo / "tests").mkdir()
    (env.repo / "tests" / "conftest.py").write_text('''
def pytest_addoption(parser):
    parser.addoption("--all")
def pytest_collection_modifyitems(config, items):
    # integration, legacy, slow are normally deselected
    if config.getoption("--all"):
        return
''')
    (env.repo / "scripts").mkdir()
    (env.repo / "scripts" / "validate.sh").write_text("python3 -m pytest tests/ --all\n")
    env.git("add", ".")
    env.git("commit", "-qm", "tiered project")
    bare, _, status = _run(env, monkeypatch, _plan("merge"))
    # Task acceptance and integration composition succeed, but no run-level
    # command proved the extra tiers. Both status and ask-mode must disclose it.
    code, report = env.office("status")
    assert code == 0 and "FULL SUITE NOT VERIFIED" in report, report
    main = remote_head(bare, "main")
    code, out = env.office("land")
    assert code == 4 and "integration-coverage-incomplete" in out, out
    assert "integration, legacy, slow" in out and "scripts/validate.sh" in out, out
    assert remote_head(bare, "main") == main
    assert all(p["state"] == "open" for p in gh(env)["prs"])

    # A handoff close does not merge, but its terminal output and durable
    # archive must still warn the recipient that the full tiers were omitted.
    code, close = env.office("close", "--handoff", "https://github.com/o/r/pull/1")
    assert code == 0 and "FULL SUITE NOT VERIFIED" in close, close
    con = env.con()
    try:
        state_dir = con.execute("SELECT state_dir FROM runs LIMIT 1").fetchone()[0]
    finally:
        con.close()
    receipt = Path(state_dir) / "archive-receipt.json"
    assert receipt.exists()
    import json
    assert json.loads(receipt.read_text())["verification"]["status"] == "partial"

def test_issue_563_unreachable_script_pytest_is_not_full(tiered_repo):
    root, sha = tiered_repo
    script = root / "scripts" / "validate.sh"
    script.write_text("#!/bin/sh\nexit 0\npython3 -m pytest tests/ -q --all\n")
    subprocess.run(["git", "-C", str(root), "add", "scripts/validate.sh"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "unreachable test"], check=True)
    sha = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    assert vc.audit(root, sha, ["scripts/validate.sh"])[0] == vc.PARTIAL
    assert vc.supplemental_checks(root, sha, ["scripts/validate.sh"]) == [vc._ATTEST_PYTEST]

@pytest.mark.review_contract("v3.1")
def test_issue_563_full_suite_proof_survives_independent_integration_review(env):
    """Checks PASS + separate review PASS must keep the checks gate, command and receipt."""
    from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run
    from office import integration, lifecycle, state

    (env.repo / "tests").mkdir()
    (env.repo / "tests" / "conftest.py").write_text(
        'def pytest_addoption(parser):\n'
        '    parser.addoption("--all", action="store_true")\n'
        'def pytest_collection_modifyitems(config, items):\n'
        '    if config.getoption("--all"):\n'
        '        return\n'
        '# integration, legacy, slow are normally deselected\n')
    env.git("add", "-A")
    env.git("commit", "-qm", "fixture: tiered test collection")
    # An isolated no-op pytest executable proves which command Office actually
    # invoked without recursively running this repo's real pytest suite.
    (env.bin / "pytest").write_text("#!/bin/sh\necho direct test command executed\nexit 0\n")
    (env.bin / "pytest").chmod(0o755)
    plan = PLAN_TWO.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: pytest tests/ --all -q\n")
    plan = plan.replace('scope: mul.py\ndepends: none', 'scope: mul.py\ndepends: T1')
    approved_run(env, plan=plan,
                 executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD},
                                              "T2": {"mul.py": GOOD_MUL}}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}],
                 integration_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2", check=0)
    assert code == 0, out
    con = env.con()
    run_id = con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    run = state.get_run(con, run_id)
    artifact = (run["landing"] or {}).get("integration") or {}
    assert artifact["status"] == "accepted", artifact
    check = con.execute("SELECT id, verdict FROM gates WHERE kind='checks' AND input_key=?",
                        ("integration:" + artifact["commit"],)).fetchone()
    review = con.execute("SELECT id, verdict FROM gates WHERE kind='integration_review'").fetchone()
    assert check and check["verdict"] == "PASS"
    assert review and review["verdict"] == "PASS"
    assert check["id"] != review["id"]
    proof = artifact.get("full_suite")
    assert proof and proof["gate"] == check["id"] and proof["commit"] == artifact["commit"], artifact
    assert vc.attested_checks(con, run, artifact["commit"]) == ["pytest tests/ --all -q"]
    assert lifecycle._verified_test_tiers(con, run)["status"] == vc.FULL
    assert "FULL SUITE NOT VERIFIED" not in "\n".join(land._ask(con, run).lines)
    code, output = env.office("close", "--handoff", "https://github.com/example/repo/pull/1")
    assert code == 0, output
    state_dir = con.execute("SELECT state_dir FROM runs LIMIT 1").fetchone()[0]
    import json
    receipt = json.loads((Path(state_dir) / "archive-receipt.json").read_text())
    assert receipt["verification"]["status"] == vc.FULL
    assert "FULL SUITE NOT VERIFIED" not in output

@pytest.mark.review_contract("v3.1")
def test_scoped_gate_real_execution_attests_composed_commit_without_full_suite(env, monkeypatch):
    """A passing selected test must yield a scoped, hash-checked closeout receipt."""
    import json
    from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run
    from office import integration, lifecycle, state

    tests = env.repo / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text('''
def pytest_addoption(parser):
    parser.addoption("--all", action="store_true")
def pytest_collection_modifyitems(config, items):
    if config.getoption("--all"):
        return
    # integration, legacy and slow are normally deselected
''')
    (tests / "test_smoke.py").write_text('def test_scoped_smoke(): assert 2 + 2 == 4\n')
    scripts = env.repo / "scripts"
    scripts.mkdir()
    (scripts / "select_pr_tests.py").write_text('''import argparse
p=argparse.ArgumentParser()
p.add_argument("--output")
a=p.parse_args()
open(a.output,"w").write("tests/test_smoke.py\\n")
''')
    (scripts / "validate.sh").write_text('''#!/usr/bin/env bash
set -e
selection_file="$(mktemp)"
python3 scripts/select_pr_tests.py --output "$selection_file"
selected_tests=()
while IFS= read -r selected_test; do selected_tests+=("$selected_test"); done < "$selection_file"
python3 -m pytest "${selected_tests[@]}" -q -n 2 --all
''')
    (scripts / "validate.sh").chmod(0o755)
    # Exercise the lifecycle with a deliberately small test-only trusted gate.
    import hashlib
    write_scoped_gate_artifacts(env.repo)
    monkeypatch.setattr(vc, "_SCOPED_GATE_SHA256", {
        p: hashlib.sha256((env.repo / p).read_bytes()).hexdigest()
        for p in vc._SCOPED_GATE_SHA256
    })
    env.git("add", "-A")
    env.git("commit", "-qm", "fixture: actual scoped validation")
    plan = PLAN_TWO.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: scripts/validate.sh --pr\n")
    plan = plan.replace('scope: mul.py\ndepends: none', 'scope: mul.py\ndepends: T1')
    approved_run(env, plan=plan,
                 executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD},
                                              "T2": {"mul.py": GOOD_MUL}}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}],
                 integration_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2", check=0)
    assert code == 0, out
    con = env.con()
    run_id = con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    run = state.get_run(con, run_id)
    artifact = (run["landing"] or {}).get("integration") or {}
    assert artifact["status"] == "accepted", artifact
    assert artifact["full_suite"]["scope"] == vc.SCOPED
    assert vc.attested_checks(con, run, artifact["commit"]) == ["scripts/validate.sh --pr"]
    land._require_run_coverage(run, artifact["commit"], con=con)
    assert lifecycle._verified_test_tiers(con, run)["status"] == vc.SCOPED
    assert "SCOPED PR TESTS VERIFIED" in "\n".join(land._ask(con, run).lines)
    code, output = env.office("close", "--handoff", "https://github.com/example/repo/pull/1")
    assert code == 0, output
    state_dir = con.execute("SELECT state_dir FROM runs LIMIT 1").fetchone()[0]
    receipt = json.loads((Path(state_dir) / "archive-receipt.json").read_text())
    assert receipt["verification"]["status"] == vc.SCOPED
    assert "SCOPED PR TESTS VERIFIED" in output


@pytest.mark.review_contract("v3.1")
def test_pinned_gate_executes_for_composed_commit(env, monkeypatch):
    """Run the actual pinned gate and selector through Office integration."""
    import hashlib
    import shutil
    from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run
    from office import state

    source = Path(__file__).resolve().parents[2]
    tests = env.repo / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text('''
def pytest_addoption(parser):
    parser.addoption("--all", action="store_true")
def pytest_collection_modifyitems(config, items):
    if config.getoption("--all"):
        return
    # integration, legacy and slow are normally deselected
''')
    for name in ("test_version_single_source.py", "test_pr_test_selection.py", "v31/test_verification_coverage.py",
                 "v31/test_issue_563_artifact_integrity.py"):
        path = tests / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_fixture_smoke(): assert True\n")
    contract = tests / "contracts" / "test_fixture_contract.py"
    contract.parent.mkdir(parents=True)
    contract.write_text("def test_fixture_contract(): assert True\n")
    scripts = env.repo / "scripts"
    scripts.mkdir()
    for name in ("validate.sh", "select_pr_tests.py"):
        shutil.copy2(source / "scripts" / name, scripts / name)
    hook = env.repo / ".githooks" / "pre-push"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    # This lifecycle test uses a tiny fixture suite; the production digest pin
    # is checked independently by test_scoped_gate_pins_match_reviewed_source.
    import hashlib
    write_scoped_gate_artifacts(env.repo)
    monkeypatch.setattr(vc, "_SCOPED_GATE_SHA256", {
        p: hashlib.sha256((env.repo / p).read_bytes()).hexdigest()
        for p in vc._SCOPED_GATE_SHA256
    })
    # Keep the gate's surrounding ecosystem checks lightweight in this fixture.
    (scripts / "check_ecosystem.py").write_text("print('fixture ecosystem')\n")
    (scripts / "office_runtime.py").write_text("print('fixture adapter')\n")
    (env.repo / "adapters/seed").mkdir(parents=True)
    (env.repo / "adapters/seed/fixture.yaml").write_text("id: fixture\n")
    shutil.copy2(source / "pyproject.toml", env.repo / "pyproject.toml")
    fixture_venv = env.tmp / "gate-venv"
    fixture_venv.mkdir()
    (fixture_venv / "bin").symlink_to(source / ".venv/bin", target_is_directory=True)
    (fixture_venv / "lib").symlink_to(source / ".venv/lib", target_is_directory=True)
    shutil.copy2(source / ".venv/pyvenv.cfg", fixture_venv / "pyvenv.cfg")
    (fixture_venv / ".validate-stamp").write_text(
        hashlib.sha256((env.repo / "pyproject.toml").read_bytes()).hexdigest() + "\n")
    (env.repo / ".venv").symlink_to(fixture_venv, target_is_directory=True)
    env.git("add", "-A")
    env.git("commit", "-qm", "fixture: pinned gate")
    env.git("update-ref", "refs/remotes/origin/main", "HEAD")
    plan = PLAN_TWO.replace("blast_radius: repo\n", "blast_radius: repo\nchecks: scripts/validate.sh --pr\n")
    plan = plan.replace('scope: mul.py\ndepends: none', 'scope: mul.py\ndepends: T1')
    approved_run(env, plan=plan,
                 executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD},
                                              "T2": {"mul.py": GOOD_MUL}}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}],
                 integration_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2", check=0)
    assert code == 0, out
    con = env.con()
    run_id = con.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    run = state.get_run(con, run_id)
    artifact = (run["landing"] or {}).get("integration") or {}
    logs = list(env.state.rglob("check-*.log"))
    assert artifact["status"] == "accepted", (artifact, [p.read_text() for p in logs])
    assert any(re.search(r"\b5 passed\b", p.read_text()) and "== Validation passed" in p.read_text() for p in logs), [p.read_text() for p in logs]
    assert artifact["full_suite"]["scope"] == vc.SCOPED
    assert vc.attested_checks(con, run, artifact["commit"]) == ["scripts/validate.sh --pr"]
    land._require_run_coverage(run, artifact["commit"], con=con)
