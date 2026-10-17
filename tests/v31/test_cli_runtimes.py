"""#633: small, editable CLI-runtime index with catalog-derived model lists.

All command tests use isolated user homes; none invokes a real harness or
mutates the shipped catalog, the maintainer's preferences, or trust records.
"""
from __future__ import annotations

import json

import pytest
import yaml

from office import adapters, candidates, cli_runtimes, harnesscmd, paths, user_catalog


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "home" / "config.yaml"))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OFFICE_USER_CATALOG", str(tmp_path / "home" / "catalog.yaml"))
    monkeypatch.setenv("OFFICE_USER_ADAPTERS", str(tmp_path / "home" / "adapters"))
    monkeypatch.delenv("OFFICE_CLI_RUNTIMES", raising=False)
    return tmp_path


def test_packaged_index_is_exact_three_column_tsv():
    raw = cli_runtimes.packaged_default().read_text(encoding="utf-8")
    entries = cli_runtimes.parse(raw)
    assert raw.splitlines()[0] == "key\tskill_path\tmodels_path"
    assert {e.key for e in entries} == {"claude", "codex", "agy", "pi", "hermes", "kilo", "opencode", "gemini"}
    assert all(e.models_path == f"models/{e.key}.json" for e in entries)
    assert raw == cli_runtimes.render(entries)


def test_sync_saves_manifest_and_canonical_models_without_modifying_seed(isolated):
    before = (paths.resources_root() / "catalog" / "seed.yaml").read_bytes()
    result = harnesscmd.manifest_list()
    assert result.exit_code == 0 and result.data["saved"] is False
    assert not cli_runtimes.location().exists()  # a list or doctor must not write anything
    saved = harnesscmd.manifest_sync()
    assert saved.exit_code == 0, saved.lines
    entries, yes = cli_runtimes.load()
    assert yes and len(entries) == 8
    statuses, yes = cli_runtimes.statuses()
    assert yes
    # More harnesses may have been registered since this draft was created.
    assert {"claude", "codex", "agy"} <= {e["key"] for e in statuses if e["configured"]}
    assert next(e for e in statuses if e["key"] == "opencode")["adapter"] == "registered"
    assert next(e for e in statuses if e["key"] == "pi")["skill"] == "missing"
    for entry in entries:
        model_file = cli_runtimes.resolve(entry, "models")
        actual = json.loads(model_file.read_text(encoding="utf-8"))
        assert actual == cli_runtimes.projection(candidates.catalog_rows(), entry.key)
        assert actual["kind"] == "office-derived-harness-model-list"
        assert all("user_denied" not in row and "trust" not in row for row in actual["models"])
    assert (paths.resources_root() / "catalog" / "seed.yaml").read_bytes() == before
    first = {e.key: cli_runtimes.resolve(e, "models").stat().st_mtime_ns for e in entries}
    manifest_bytes = cli_runtimes.location().read_bytes()
    assert harnesscmd.manifest_sync().exit_code == 0
    assert cli_runtimes.location().read_bytes() == manifest_bytes
    assert first == {e.key: cli_runtimes.resolve(e, "models").stat().st_mtime_ns for e in entries}


def test_strict_tsv_validation_never_replaces_broken_user_file(isolated):
    bads = (
        "key,skill_path,models_path\nclaude,a,b\n",
        "key\tskill_path\tmodels_path\nclaude\ta\tb\nclaude\tc\td\n",
        "key\tskill_path\tmodels_path\nclaude\ta\tb\textra\n",
        "key\tskill_path\tmodels_path\nCLAUDE\ta\tb\n",
        "key\tskill_path\tmodels_path\nclaude\tskills/../bad\tmodels/claude.json\n",
    )
    p = cli_runtimes.location()
    p.parent.mkdir(parents=True)
    for bad in bads:
        p.write_text(bad, encoding="utf-8")
        res = harnesscmd.manifest_sync()
        assert res.exit_code == 1 and "refused" in res.lines[0]
        assert p.read_text(encoding="utf-8") == bad
        assert not (p.parent / "models").exists()


def test_custom_paths_are_read_only_and_comments_survive_sync(isolated):
    p = cli_runtimes.location()
    p.parent.mkdir(parents=True)
    custom = isolated / "models-external.json"
    custom.write_text('{"untouched":true}\n', encoding="utf-8")
    manual = ("# person-authored comment\nkey\tskill_path\tmodels_path\n"
              f"claude\tskills/claude-cli/SKILL.md\t{custom}\n")
    p.write_text(manual, encoding="utf-8")
    result = harnesscmd.manifest_sync()
    assert result.exit_code == 0 and any("custom models_path" in l for l in result.lines)
    assert custom.read_text(encoding="utf-8") == '{"untouched":true}\n'
    assert p.read_text().startswith(manual)
    # User rows are preserved; newly registered adapters are appended.
    assert "pi\tskills/pi-cli/SKILL.md\tmodels/pi.json" in p.read_text()
    assert "opencode\tskills/opencode-cli/SKILL.md\tmodels/opencode.json" in p.read_text()
    status, _ = cli_runtimes.statuses()
    assert next(r for r in status if r["key"] == "claude")["models"] == "stale"
    # A second sync without new adapters must not rewrite user comments.
    same = p.read_bytes()
    assert harnesscmd.manifest_sync().exit_code == 0
    assert p.read_bytes() == same


def test_new_user_adapter_is_appended_without_python_name_tuple(isolated):
    assert harnesscmd.manifest_sync().exit_code == 0
    d = adapters.user_adapter_dir()
    d.mkdir(parents=True, exist_ok=True)
    # Office source loader discovers valid, non-TODO user adapters, not a Python list.
    (d / "freshharness.yaml").write_text(yaml.safe_dump({
        "id": "freshharness", "verified_state": "valid-unverified",
        "invocation": {"executable": "freshharness"},
        "office_profiles": {"worker": {"argv": ["--model", "{model}"], "prompt": "stdin"}},
    }), encoding="utf-8")
    target = cli_runtimes.location()
    before = target.read_text()
    assert "freshharness" not in before
    assert harnesscmd.manifest_sync().exit_code == 0
    after = target.read_text()
    assert after.startswith(before)
    assert "freshharness\tskills/freshharness-cli/SKILL.md\tmodels/freshharness.json" in after
    status, _ = cli_runtimes.statuses()
    new = next(e for e in status if e["key"] == "freshharness")
    assert new["adapter"] == "registered" and new["skill"] == "missing"
    assert new["models"] == "ok" and new["configured"] is False


def test_catalog_overlay_changes_stale_status_until_refresh(isolated):
    assert harnesscmd.manifest_sync().exit_code == 0
    before, _ = cli_runtimes.statuses()
    assert next(r for r in before if r["key"] == "codex")["models"] == "ok"
    user_catalog.save({"models": [], "disabled": [{"harness": "codex", "model_id": "luna"}]})
    status, _ = cli_runtimes.statuses()
    assert next(r for r in status if r["key"] == "codex")["models"] == "stale"
    assert cli_runtimes.refresh_if_saved() == "CLI model projections refreshed: 1"
    refreshed, _ = cli_runtimes.statuses()
    assert next(r for r in refreshed if r["key"] == "codex")["models"] == "ok"
    # The disabled row follows authoritative catalog and is not an independently editable policy.
    models = json.loads((cli_runtimes.location().parent / "models" / "codex.json").read_text())["models"]
    assert any(x["model_id"] == "luna" and x["dispatchable"] is False for x in models)


def test_generated_directory_symlink_is_refused_without_creating_manifest(isolated):
    home = cli_runtimes.location().parent
    home.mkdir(parents=True)
    external = isolated / "other"
    external.mkdir()
    (home / "models").symlink_to(external, target_is_directory=True)
    res = harnesscmd.manifest_sync()
    assert res.exit_code == 1 and "symlinked" in res.lines[0]
    assert not cli_runtimes.location().exists()
    assert list(external.iterdir()) == []


def test_harness_commands_report_saved_index_and_missing_skills(env):
    # env.office launches a real CLI process inside a private test home, not a model.
    code, out = env.office("harness", "manifest")
    assert code == 0 and "not saved" in out
    code, out = env.office("harness", "sync")
    assert code == 0, out
    code, out = env.office("harness", "list")
    assert code == 0 and "CLI runtimes:" in out and "opencode" in out and "unconfigured" not in out
    assert "skill missing" in out and "models ok" in out
    code, out = env.office("harness", "manifest", "--json")
    assert code == 0
    # CLI JSON is stable enough to provide machine-readable locations.
    assert "models_path" in out and "skill_path" in out


def test_model_commands_refresh_managed_views_without_touching_trust(isolated):
    assert harnesscmd.manifest_sync().exit_code == 0
    added = harnesscmd.model_add("agy/acme/fresh-model", ["medium"])
    assert "CLI model projections refreshed: 1" in "\n".join(added.lines)
    assert added.exit_code == 0
    view = json.loads((cli_runtimes.location().parent / "models" / "agy.json").read_text())
    assert any(r["model_id"] == "fresh-model" and r["invocation_model_id"] == "acme/fresh-model"
               for r in view["models"])
    disabled = harnesscmd.model_disable("agy/fresh-model")
    assert disabled.exit_code == 0
    view = json.loads((cli_runtimes.location().parent / "models" / "agy.json").read_text())
    assert any(r["model_id"] == "fresh-model" and r["dispatchable"] is False for r in view["models"])
    assert next(r for r in cli_runtimes.statuses()[0] if r["key"] == "agy")["models"] == "ok"
    enabled = harnesscmd.model_enable("agy/fresh-model")
    assert enabled.exit_code == 0
    view = json.loads((cli_runtimes.location().parent / "models" / "agy.json").read_text())
    assert any(r["model_id"] == "fresh-model" and r["dispatchable"] is True for r in view["models"])
    assert "trust" not in view and "policy" not in view


def test_user_manifest_path_override_and_packaged_skill_resolution(isolated, monkeypatch):
    custom_dir = isolated / "elsewhere"
    monkeypatch.setenv("OFFICE_CLI_RUNTIMES", str(custom_dir / "index.tsv"))
    p, count, _, _ = cli_runtimes.sync()
    assert p == custom_dir / "index.tsv" and count == 8
    assert (custom_dir / "models" / "claude.json").is_file()
    entries, yes = cli_runtimes.load()
    assert yes
    # A bundled skill must resolve independently of the manifest's location.
    assert cli_runtimes.resolve(entries[0], "skill") == paths.resources_root() / "skills/claude-cli/SKILL.md"


def test_doctor_reports_corrupt_mapping_without_repairing_it(env):
    # doctor should report a corrupt user TSV, never replace it or grant trust.
    target = cli_runtimes.location()
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = "key\tskill_path\tmodels_path\nclaude\twrong\n"
    target.write_text(raw, encoding="utf-8")
    code, out = env.office("doctor")
    assert "CLI runtime TSV: invalid:" in out and "repair the TSV" in out
    assert target.read_text() == raw


@pytest.mark.parametrize(("field", "replacement"), [
    ("invocation_source", "unverified: invocation evidence revoked"),
    ("source_name", "withdrawn-source"),
    ("source_effort", "unverified effort"),
    ("effort_confidence", "unverified"),
    ("source_snapshot_metadata", {"provenance": "revoked"}),
])
def test_change_to_technical_provenance_marks_projection_stale(isolated, monkeypatch, field, replacement):
    assert harnesscmd.manifest_sync().exit_code == 0
    model_file = cli_runtimes.location().parent / "models" / "claude.json"
    initial = json.loads(model_file.read_text(encoding="utf-8"))
    original_rows = candidates.catalog_rows()
    changed = [dict(row) for row in original_rows]
    row = next(row for row in changed if row.get("invocation_harness") == "claude")
    assert row.get(field) != replacement
    row[field] = replacement
    monkeypatch.setattr(candidates, "catalog_rows", lambda: changed)

    state = next(s for s in cli_runtimes.statuses()[0] if s["key"] == "claude")
    assert state["models"] == "stale"
    assert state["configured"] is False
    assert initial["content_hash"] != cli_runtimes.projection(changed, "claude")["content_hash"]
    assert cli_runtimes.refresh_if_saved() == "CLI model projections refreshed: 1"
    after = json.loads(model_file.read_text(encoding="utf-8"))
    assert after["models"] != initial["models"]
    assert next(s for s in cli_runtimes.statuses()[0] if s["key"] == "claude")["models"] == "ok"


def test_unreadable_skill_never_reports_configured(isolated):
    assert harnesscmd.manifest_sync().exit_code == 0
    path = isolated / "unreadable-skill.md"
    path.write_text("Available", encoding="utf-8")
    entries, _ = cli_runtimes.load()
    changed = [cli_runtimes.Entry(e.key, str(path) if e.key == "claude" else e.skill_path,
                                 e.models_path) for e in entries]
    cli_runtimes.location().write_text(cli_runtimes.render(changed), encoding="utf-8")
    path.chmod(0)
    try:
        state = next(s for s in cli_runtimes.statuses()[0] if s["key"] == "claude")
        assert state["skill"] == "unreadable"
        assert state["models"] == "ok"
        assert state["adapter"] == "registered"
        assert state["configured"] is False
    finally:
        path.chmod(0o600)
