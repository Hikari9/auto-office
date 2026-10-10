"""#500: launch argv is inspectable without recording prompts or credentials."""
from office import dispatch


def test_redact_argument_secrets_without_losing_launch_shape():
    assert dispatch._redact_launch_argv([
        "codex", "exec", "--model", "gpt", "--api-key=private",
        "--token", "secret-value", "-c", "projects={trusted=true}",
        "ghp_" + "a" * 36,
    ]) == [
        "codex", "exec", "--model", "gpt", "--api-key=[REDACTED]",
        "--token", "[REDACTED]", "-c", "projects={trusted=true}",
        "[REDACTED]",
    ]


def test_redaction_adversarial_combinations():
    sk, gh = "sk-ant-" + "x" * 30, "ghs_" + "b" * 36
    out = dispatch._redact_launch_argv([
        "env", f"OPENAI_API_KEY={sk}", "ANTHROPIC_AUTH_TOKEN=abc123", "AWS_SECRET_ACCESS_KEY=zz",
        "-c", 'mcp_servers.x.env.API_KEY="v1"', "--header", "Authorization: Bearer v2",
        "--remote", f"https://user:{gh}@github.com/o/r.git", f"--extra={gh}", "--password",
        "hunter2", "--cookie=c1", "AKIAABCDEFGHIJKLMNOP", "--max-tokens", "4096",
        "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.sig_part",
    ])
    flat = " ".join(out)
    for leaked in (sk, gh, "abc123", "zz", "v1", "v2", "hunter2", "c1", "AKIAABCDEFGHIJKLMNOP", "eyJhbGci", "user:"):
        assert leaked not in flat, leaked
    # The launch shape survives: names, flags, hosts and harmless values stay readable.
    for kept in ("OPENAI_API_KEY=", "Authorization:", "--password", "github.com/o/r.git", "--max-tokens", "4096"):
        assert kept in flat, kept


def test_launch_spec_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda _: tmp_path)
    dispatch.write_launch_spec({"id": "R1"}, "D1", {"argv": []})
    assert (tmp_path / "dispatches" / "D1" / "launch.json").stat().st_mode & 0o077 == 0


def test_record_both_forms_preserves_persisted_launch_evidence(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda _: tmp_path)
    monkeypatch.setattr(dispatch, "atomic_write_json", lambda path, value, mode=None: written.append((path, dict(value))))
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


def test_redaction_secret_after_first_separator_and_generic_flags():
    out = dispatch._redact_launch_argv([
        "--header=Cookie=abc1", "-c", '{"model":"x","token":"abc2"}', "--opt", "a=b,api_key=abc3",
        "--auth", "abc4", "--password:abc9", "--key", "abc5", "--pat", "abc6", "-p", "the raw prompt", "--env", "FOO=abc7",
        "-e", "BAR=abc8", "--model", "gpt-5",
    ])
    flat = " ".join(out)
    for leaked in ("abc1", "abc2", "abc3", "abc4", "abc5", "abc6", "raw prompt", "abc7", "abc8", "abc9"):
        assert leaked not in flat, leaked
    assert "FOO=[REDACTED]" in out and '"model":"x"' in flat and out[-2:] == ["--model", "gpt-5"]
