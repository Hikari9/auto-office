"""Real CLI process smokes use a scripted fake harness and isolated user homes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


FAKE = f'''#!{sys.executable}
import os, sys
from pathlib import Path
import json
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("fakecli 1.0"); raise SystemExit(0)
if args[:1] == ["models"]:
    print("acme/fast-1"); raise SystemExit(0)
prompt = sys.stdin.read()
Path(os.environ["OFFICE_SMOKE_CAPTURE"]).write_text(json.dumps({{"argv": args, "prompt": prompt}}))
print("READY")
'''

ADAPTER = {
    "id": "fakecli", "verified_state": "valid-unverified",
    "version_fingerprint": {"command": ["fakecli", "--version"], "parser": "first-line"},
    "model_source": {"type": "cli", "command": ["fakecli", "models"]},
    "preflight": {"model_check": ["fakecli", "models"]},
    "effort_mapping": {"low": "low", "medium": "medium", "high": "high"},
    "benchmark_slug_mapping": {},
    "invocation": {"executable": "fakecli", "argv": ["--model", "{model}", "--effort", "{effort}"],
                   "prompt_transport": "stdin", "cwd_transport": "process-cwd"},
    "safe_prompt_passing": {"shell": False, "supports_prompt_file": False, "notes": "stdin only"},
    "dispatch_forms": ["cli"], "trusted_for": ["discovery"],
    "quota_probe": {"type": "external-primitive", "command": None, "unknown_is_unlimited": False},
    "shallow_review": {"supported": False, "minimum_effort": None},
    "agentic_capability": {"default": "unknown"}, "failure_signatures": [],
    "conformance": {"deterministic": "pending", "live": "pending"}, "source_notes": "scripted fake",
    "session": {"id": "none"}, "capabilities": ["builder"],
    "office_profiles": {"worker": {"argv": ["--model", "{model}", "--effort", "{effort}"],
                                    "prompt": "stdin", "output": "stdout"}},
}


@pytest.fixture
def cli_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "fakecli"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    (adapters / "fakecli.yaml").write_text(yaml.safe_dump(ADAPTER, sort_keys=False))
    env = os.environ.copy()
    env.update({"OFFICE_USER_CONFIG": str(tmp_path / "config.yaml"),
                "OFFICE_USER_ADAPTERS": str(adapters), "OFFICE_DATA_HOME": str(tmp_path / "data"),
                "OFFICE_STATE_HOME": str(tmp_path / "state"), "AUTO_OFFICE_RUNS_DB": str(tmp_path / "data/runs.db"),
                "OFFICE_SMOKE_CAPTURE": str(tmp_path / "smoke-capture.json"),
                "PATH": os.pathsep.join([str(bindir), env.get("PATH", "")])})
    return env


def _office(cli_env, *args):
    command = shutil.which("office")
    assert command, "the repository's Office CLI must be installed for process contract tests"
    return subprocess.run([command, *args], env=cli_env, text=True, capture_output=True, timeout=30)


def test_cli_validates_a_user_adapter_in_an_isolated_home(cli_env):
    result = _office(cli_env, "harness", "validate", "fakecli", "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["data"]["valid"] is True


def test_cli_smokes_only_the_scripted_fake_harness(cli_env):
    result = _office(cli_env, "harness", "smoke", "fakecli", "--model", "acme/fast-1",
                     "--effort", "medium", "--timeout", "10", "--json")
    assert result.returncode == 0, result.stderr + result.stdout
    record = json.loads(result.stdout)["data"]
    assert record["result"] == "launched" and record["trust"] == "unchanged"
    assert (Path(cli_env["OFFICE_DATA_HOME"]) / "harness-smoke.jsonl").is_file()
    launch = json.loads(Path(cli_env["OFFICE_SMOKE_CAPTURE"]).read_text())
    assert launch["argv"] == ["--model", "acme/fast-1", "--effort", "medium"]
    assert launch["prompt"].strip()


def test_cli_model_registration_and_policy_allow_are_read_back(cli_env):
    added = _office(cli_env, "model", "add", "fakecli/acme/fast-1", "--effort", "medium", "--json")
    assert added.returncode == 0, added.stderr + added.stdout
    allowed = _office(cli_env, "route", "allow", "fakecli/fast-1@medium", "--role", "executor", "--json")
    assert allowed.returncode == 0, allowed.stderr + allowed.stdout
    config = yaml.safe_load(Path(cli_env["OFFICE_USER_CONFIG"]).read_text())
    assert config["routing"]["user_policy"]["role_rules"] == [
        {"role": "executor", "route": "fakecli@1/fast-1@medium", "state": "allow"}]
    effective = _office(cli_env, "route", "policy", "--effective", "--role", "executor", "--json")
    assert effective.returncode == 0, effective.stderr + effective.stdout
    rows = json.loads(effective.stdout)["data"]["effective"]
    route = next(row for row in rows if row["route"].endswith("/fast-1@medium"))
    assert route["state"] == "allow"
