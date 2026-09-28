"""A wheel says "3.1.0" before and after a fix lands; install_drift compares content instead."""
import json
import shutil

from office import version


def _source(tmp_path):
    src = tmp_path / "src-checkout"
    (src / "src" / "office").mkdir(parents=True)
    (src / "src" / "office" / "dispatch.py").write_text("SPLIT = True\n")
    (src / "adapters").mkdir()
    (src / "adapters" / "codex.yaml").write_text("id: codex\n")
    (src / "SKILL.md").write_text("# skill\n")
    (src / "VERSION").write_text("3.1.0\n")
    return src


def _wheel_from(src, tmp_path):
    """Lay the source out the way the wheel's force-include does."""
    pkg = tmp_path / "site-packages" / "office"
    shutil.copytree(src / "src" / "office", pkg)
    res = pkg / "_resources"
    shutil.copytree(src / "adapters", res / "adapters")
    shutil.copy(src / "SKILL.md", res / "SKILL.md")
    shutil.copy(src / "VERSION", res / "VERSION")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "dispatch.cpython-314.pyc").write_bytes(b"\0")
    return pkg


def test_fresh_install_matches_source(tmp_path):
    src = _source(tmp_path)
    pkg = _wheel_from(src, tmp_path)
    drift = version.install_drift(pkg, src)
    assert drift["differ"] == []
    assert drift["source"] == str(src)


def test_fix_landed_after_install_is_reported(tmp_path):
    src = _source(tmp_path)
    pkg = _wheel_from(src, tmp_path)
    (src / "src" / "office" / "dispatch.py").write_text("SPLIT = True\nRENAME = True\n")
    (src / "adapters" / "agy.yaml").write_text("id: agy\n")
    drift = version.install_drift(pkg, src)
    assert drift["differ"] == ["adapters/agy.yaml", "src/office/dispatch.py"]


def test_file_removed_from_source_is_reported(tmp_path):
    src = _source(tmp_path)
    pkg = _wheel_from(src, tmp_path)
    (src / "SKILL.md").unlink()
    assert version.install_drift(pkg, src)["differ"] == ["SKILL.md"]


def test_no_local_source_means_nothing_to_compare(tmp_path):
    pkg = _wheel_from(_source(tmp_path), tmp_path)
    assert version.install_drift(pkg, tmp_path / "missing") is None


def test_install_source_reads_direct_url(tmp_path, monkeypatch):
    src = _source(tmp_path)

    class Dist:
        def __init__(self, payload):
            self.payload = payload

        def read_text(self, name):
            return json.dumps(self.payload) if name == "direct_url.json" else None

    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: Dist({"url": src.as_uri(), "dir_info": {}}))
    assert version.install_source() == src
    monkeypatch.setattr(md, "distribution", lambda name: Dist({"url": src.as_uri(), "dir_info": {"editable": True}}))
    assert version.install_source() is None
    monkeypatch.setattr(md, "distribution", lambda name: Dist({"url": "https://example.invalid/x.whl"}))
    assert version.install_source() is None
