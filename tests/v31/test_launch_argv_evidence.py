"""#500: launch argv is inspectable without recording prompts or credentials."""
from office import dispatch


def test_redact_argument_secrets_without_losing_launch_shape():
    assert dispatch._redact_launch_argv([
        "codex", "exec", "--model", "gpt", "--api-key=private",
        "--token", "secret-value", "-c", "projects={trusted=true}",
        "ghp_12345",
    ]) == [
        "codex", "exec", "--model", "gpt", "[REDACTED]",
        "--token", "[REDACTED]", "-c", "projects={trusted=true}",
        "[REDACTED]",
    ]


def test_record_both_forms_preserves_persisted_launch_evidence(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda _: tmp_path)
    monkeypatch.setattr(dispatch, "atomic_write_json", lambda path, value: written.append((path, dict(value))))
    monkeypatch.setattr(dispatch.adapters, "load_all", lambda: {"mock": {"id": "mock"}})
    run = {"id": "R1"}
    d = {"id": "D1", "adapter_id": "mock", "route": {"candidate": {"harness_version": "1.2.3"}}}
    spec = {"prompt_file": "/tmp/sensitive-prompt"}
    dispatch._record_launch_form(run, d, spec, "herdr", ["herdr", "agent", "start", "mock"],
                                 transport="herdr agent prompt pointer", herdr_kind="mock")
    dispatch._record_launch_form(run, d, spec, "headless",
                                 ["mock", "run", "[PROMPT REDACTED]"],
                                 transport="argv", output="/tmp/reply.txt")
    forms = written[-1][1]["rendered_launches"]
    assert set(forms) == {"herdr", "headless"}
    assert forms["herdr"]["harness_version"] == "1.2.3"
    assert forms["headless"]["prompt_transport"] == "argv"
    assert forms["headless"]["argv"][-1] == "[PROMPT REDACTED]"
    assert "sensitive-prompt" not in str(forms)
