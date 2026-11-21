"""Onboarding harness-preference round (T4): overlap detection, due-once, budget-derived weights, and the
Hermes auth listing in `office onboard --json`. Fake harness binaries and a fixed Hermes auth list, so no real
harness or provider is consulted."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from office import candidates, cli, discovery, onboarding
from office import hermes_access

OFFICE_AUTH = {"claude": "found", "codex": "found", "agy": "found", "hermes": "found"}
VERSIONS = {"claude": "2.1.300", "codex": "0.160.0", "agy": "1.3.2", "hermes": "0.9.0"}
# Hermes reaches anthropic with a usable credential and openrouter with only an exhausted one.
HERMES_AUTH = {
    "anthropic": {"credentials": 1, "usable": 1, "exhausted": 0},
    "openrouter": {"credentials": 1, "usable": 0, "exhausted": 1},
}
# Installed claude, agy and hermes all run claude-sonnet-5-5 on anthropic: the overlap this round answers.
OVERLAP_HARNESSES = ["agy", "claude", "hermes"]


class Home:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.user = tmp / "user-config.yaml"
        self.bin = tmp / "bin"

    def office(self, *args) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = cli.main(list(args))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else 1
        return code, out.getvalue()

    def ojson(self, *args) -> tuple[int, dict]:
        code, out = self.office(*args, "--json")
        return code, json.loads(out[out.index("{"):])

    def status(self) -> dict:
        code, data = self.ojson("onboard", "--harness", "claude")
        assert code == 0, data
        return data["data"]

    def user_data(self) -> dict:
        return yaml.safe_load(self.user.read_text()) if self.user.exists() else {}

    def remove(self, name: str) -> None:
        (self.bin / name).unlink()


@pytest.fixture
def h(tmp_path, monkeypatch):
    t = Home(tmp_path)
    t.bin.mkdir()
    for name, ver in VERSIONS.items():
        exe = t.bin / name
        exe.write_text(f"#!/bin/sh\necho {ver}\n")
        exe.chmod(0o755)
    for k in list(os.environ):
        if k.startswith(("OFFICE_", "HERDR_", "AUTO_OFFICE_", "HERMES_")) or k in (
                "CLAUDECODE", "GEMINI_CLI", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(k, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(t.user))
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
    monkeypatch.setenv("OFFICE_AUTH_FIXTURE", json.dumps(OFFICE_AUTH))
    monkeypatch.setenv("PATH", os.pathsep.join([str(t.bin), "/usr/bin", "/bin"]))
    monkeypatch.setattr(discovery, "process_key", lambda: None)
    monkeypatch.setattr(hermes_access, "auth_status", lambda **_: HERMES_AUTH)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    return t


def answer_schema(h: Home) -> None:
    code, out = h.office("onboard", "--planner", "office", "--executor", "office", "--reviewer", "office")
    assert code == 0, out


def pref(h: Home) -> dict:
    return h.status()["harness_preference"]


def entries(hp: dict) -> list[str]:
    """The overlap set as it is stored: one provider|model|harness entry per harness running a shared model."""
    return sorted(f"{o['provider']}|{p}" for o in hp["overlap"] for p in o["pairs"])


# ------------------------------------------------------------------ detection and due-once

def test_first_overlap_makes_onboarding_due_once_with_the_overlap_listed(h):
    data = h.status()
    hp = data["harness_preference"]
    assert data["due"] is True and "harness overlap not yet answered" in data["reason"]
    assert [o["provider"] for o in hp["overlap"]] == ["anthropic"]
    assert hp["overlap"][0]["harnesses"] == OVERLAP_HARNESSES
    assert "claude-sonnet-5-5" in hp["overlap"][0]["models"]
    assert hp["pending"] == OVERLAP_HARNESSES
    answer_schema(h)
    assert h.status()["due"] is True  # the schema is current; the overlap round is still owed


def test_answering_the_round_records_the_harnesses_so_they_are_not_asked_again(h):
    answer_schema(h)
    expected = entries(pref(h))
    code, out = h.office("onboard", "--harness-preference", "keep")
    assert code == 0 and "harness preference saved" in out, out
    assert h.user_data()["onboarding"]["harness_overlap_seen"] == expected
    assert "anthropic|claude-sonnet-5-5|hermes" in expected
    data = h.status()
    assert data["due"] is False and data["reason"] == "current"
    assert pref(h)["pending"] == []


def test_a_recorded_harness_is_not_asked_again_even_when_the_overlap_narrows(h):
    answer_schema(h)
    h.office("onboard", "--harness-preference", "keep")
    h.remove("hermes")  # claude and agy still overlap, and both were recorded
    hp = pref(h)
    assert [o["harnesses"] for o in hp["overlap"]] == [["agy", "claude"]]
    assert hp["pending"] == [] and h.status()["due"] is False


def test_skip_records_the_overlap_set_so_the_round_is_not_asked_again(h):
    expected = entries(pref(h))
    code, out = h.office("onboard", "--skip")
    assert code == 0 and "preferences unchanged" in out, out
    assert h.user_data() == {"onboarding": {"schema_version": onboarding.SCHEMA_VERSION,
                                            "harness_overlap_seen": expected}}
    assert pref(h)["due"] is False and h.status()["due"] is False


def test_a_new_overlapping_harness_reopens_the_round(h):
    h.remove("hermes")
    code, out = h.office("onboard", "--skip")  # records agy and claude only
    assert code == 0, out
    assert h.status()["due"] is False
    (h.bin / "hermes").write_text("#!/bin/sh\necho 0.9.0\n")  # hermes is installed now and joins the overlap
    (h.bin / "hermes").chmod(0o755)
    hp = pref(h)
    # hermes joins the sonnet and opus overlap, and it also shares haiku with claude, which is new too
    assert hp["due"] is True and hp["pending"] == ["claude", "hermes"]
    assert h.status()["reason"].startswith("harness overlap not yet answered: claude, hermes")


def hide_hermes_haiku(monkeypatch):
    """Drop the catalog's hermes row for claude-haiku-5-5, so claude and hermes do not share haiku yet.
    Returns the real catalog function to put back with restore_catalog."""
    real_rows = candidates.catalog_rows
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [
        r for r in real_rows() if not (r.get("invocation_harness") == "hermes" and r.get("model_id") == "claude-haiku-5-5")])
    return real_rows


def restore_catalog(monkeypatch, real_rows) -> None:
    monkeypatch.setattr(candidates, "catalog_rows", real_rows)  # not monkeypatch.undo: the fixture's PATH and auth stay


def test_a_newly_shared_model_for_an_already_recorded_pair_reopens_the_round(h, monkeypatch):
    real_rows = hide_hermes_haiku(monkeypatch)
    answer_schema(h)
    h.office("onboard", "--harness-preference", "keep")
    assert pref(h)["due"] is False
    restore_catalog(monkeypatch, real_rows)  # hermes now runs claude-haiku-5-5 too: the same harnesses, a new shared model
    hp = pref(h)
    assert [o["harnesses"] for o in hp["overlap"]] == [OVERLAP_HARNESSES]
    assert "claude-haiku-5-5" in hp["overlap"][0]["models"]
    assert hp["due"] is True and hp["pending"] == ["claude", "hermes"]
    assert h.status()["reason"].startswith("harness overlap not yet answered: claude, hermes")


def test_recording_a_newly_shared_model_stores_its_entry(h, monkeypatch):
    real_rows = hide_hermes_haiku(monkeypatch)
    answer_schema(h)
    h.office("onboard", "--harness-preference", "keep")
    restore_catalog(monkeypatch, real_rows)
    code, out = h.office("onboard", "--harness-preference", "keep")
    assert code == 0, out
    stored = h.user_data()["onboarding"]["harness_overlap_seen"]
    assert "anthropic|claude-haiku-5-5|hermes" in stored and "anthropic|claude-sonnet-5-5|hermes" in stored
    assert pref(h)["due"] is False


def test_bare_harness_names_recorded_before_the_model_was_stored_still_count(h):
    h.user.write_text(yaml.safe_dump({"onboarding": {"harness_overlap_seen": OVERLAP_HARNESSES}}))
    assert pref(h)["due"] is False
    expected = entries(pref(h))
    code, out = h.office("onboard", "--skip")
    assert code == 0, out
    assert h.user_data()["onboarding"]["harness_overlap_seen"] == expected  # the entries replace the bare names


def test_no_overlap_means_no_harness_round(h):
    h.remove("hermes")
    h.remove("agy")  # claude alone: codex runs other vendors' models
    hp = h.status()["harness_preference"]
    assert hp["overlap"] == [] and hp["due"] is False


# ------------------------------------------------------------------ answers

def test_the_round_does_not_move_the_schema_marker(h):
    h.office("onboard", "--harness-preference", "keep")
    assert "schema_version" not in (h.user_data().get("onboarding") or {})
    assert h.status()["completed_schema"] == 0


def test_recommended_weights_follow_the_monthly_budgets_and_show_the_reasoning(h):
    answer_schema(h)
    budgets = ["anthropic:claude=60", "anthropic:hermes=40", "anthropic:agy=0"]
    args = ["onboard", "--harness-preference", "recommended"]
    for b in budgets:
        args += ["--harness-budget", b]
    code, out = h.office(*args)
    assert code == 0, out
    assert h.user_data()["routing"]["harness_preference"] == {"anthropic": {"claude": 60.0, "hermes": 40.0, "agy": 0.0}}
    assert "$40/mo of $100 = 40%" in out and "claude $60/mo of $100 = 60%" in out, out
    assert h.user_data()["onboarding"]["harness_budgets"] == {"anthropic": {"claude": 60.0, "hermes": 40.0, "agy": 0.0}}


def test_different_budgets_give_different_weights_so_no_split_is_hard_coded(h):
    answer_schema(h)
    code, out = h.office("onboard", "--harness-preference", "recommended",
                         "--harness-budget", "anthropic:claude=10", "--harness-budget", "anthropic:hermes=30",
                         "--harness-budget", "anthropic:agy=0")
    assert code == 0, out
    assert h.user_data()["routing"]["harness_preference"]["anthropic"] == {"claude": 25.0, "hermes": 75.0, "agy": 0.0}


def test_recommended_needs_a_budget_for_every_overlapping_pair_and_writes_nothing(h):
    before = h.user.exists()
    code, out = h.office("onboard", "--harness-preference", "recommended", "--harness-budget", "anthropic:claude=60")
    assert code == 2 and "anthropic:hermes" in out and "anthropic:agy" in out, out
    assert h.user.exists() == before and "routing" not in h.user_data()


@pytest.mark.parametrize("spec", ["anthropic:claude=-5", "anthropic:claude=nan", "anthropic:claude=inf",
                                  "anthropic:claude=lots", "anthropic:claude", "claude=60", "codex:claude=60",
                                  "anthropic:codex=60"])
def test_bad_budget_is_refused_and_nothing_is_written(h, spec):
    code, out = h.office("onboard", "--harness-preference", "recommended", "--harness-budget", spec)
    assert code == 2, out
    assert not h.user.exists() or "routing" not in h.user_data()


def test_a_budget_without_recommended_is_refused(h):
    code, out = h.office("onboard", "--harness-preference", "keep", "--harness-budget", "anthropic:claude=60")
    assert code == 2 and "recommended only" in out, out
    code, out = h.office("onboard", "--harness-budget", "anthropic:claude=60")
    assert code == 2, out
    assert not h.user.exists()


@pytest.mark.parametrize("answer", ["maybe", "Recommended", "claude/sonnet@high"])
def test_invalid_answer_writes_nothing_and_is_re_askable(h, answer):
    code, out = h.office("onboard", "--harness-preference", answer)
    assert code == 2, out
    assert not h.user.exists()
    code, out = h.office("onboard", "--harness-preference", "keep")
    assert code == 0 and pref(h)["due"] is False


def test_office_answer_clears_only_the_overlap_weights_and_keeps_other_scopes(h):
    h.user.write_text(yaml.safe_dump({"routing": {"harness_preference": {"openai": {"codex": 100}}}}))
    h.office("onboard", "--harness-preference", "recommended", "--harness-budget", "anthropic:claude=1",
             "--harness-budget", "anthropic:hermes=1", "--harness-budget", "anthropic:agy=1")
    assert h.user_data()["routing"]["harness_preference"]["anthropic"] == {"claude": 33.3, "hermes": 33.3, "agy": 33.3}
    code, out = h.office("onboard", "--harness-preference", "office")
    assert code == 0, out
    assert h.user_data()["routing"]["harness_preference"] == {"openai": {"codex": 100}}


def test_a_zero_budget_provider_gets_no_weights_and_is_reported(h):
    code, out = h.office("onboard", "--harness-preference", "recommended", "--harness-budget", "anthropic:claude=0",
                         "--harness-budget", "anthropic:hermes=0", "--harness-budget", "anthropic:agy=0")
    assert code == 0, out
    assert "routing" not in h.user_data()  # nothing to weight: no key written
    assert "no monthly budget" in out


def test_budgets_are_onboarding_input_only_and_routing_never_reads_them():
    root = Path(onboarding.__file__).resolve().parent
    readers = {p.name for p in root.glob("*.py") if "harness_budgets" in p.read_text() or "BUDGETS_KEY" in p.read_text()}
    assert readers <= {"onboarding.py", "cli.py"}  # cli.py only forwards the flag
    for routing_module in ("routing.py", "candidates.py", "config.py", "route_policy.py", "harness_preference.py"):
        assert routing_module not in readers


# ------------------------------------------------------------------ status and the Hermes auth listing

def test_hand_edited_budget_and_seen_values_do_not_break_status(h):
    h.user.write_text(yaml.safe_dump({"onboarding": {"harness_budgets": {"anthropic": "lots", "x": {"claude": -4}},
                                                     "harness_overlap_seen": "claude"}}))
    code, out = h.ojson("onboard", "--harness", "claude")
    assert code == 0, out
    hp = out["data"]["harness_preference"]
    assert hp["pending"] == OVERLAP_HARNESSES and hp["recommended"] is None and hp["budgets"]["anthropic"] == {}


def test_status_lists_hermes_providers_by_name_and_status_only(h):
    data = h.status()
    hermes = next(r for r in data["harnesses"] if r["harness"] == "hermes")
    assert hermes["providers"] == [{"provider": "anthropic", "status": "usable"},
                                   {"provider": "openrouter", "status": "exhausted"}]
    for row in hermes["providers"]:
        assert set(row) == {"provider", "status"}


def test_status_shows_the_reasoning_once_budgets_are_stored(h):
    answer_schema(h)
    h.office("onboard", "--harness-preference", "recommended", "--harness-budget", "anthropic:claude=60",
             "--harness-budget", "anthropic:hermes=40", "--harness-budget", "anthropic:agy=0")
    hp = pref(h)
    assert hp["recommended"] == {"anthropic": {"claude": 60.0, "hermes": 40.0, "agy": 0.0}}
    assert any("$40/mo of $100 = 40%" in line for line in hp["reasoning"])
    assert hp["budgets"]["anthropic"] == {"claude": 60.0, "hermes": 40.0, "agy": 0.0}


# ------------------------------------------------------------------ pure derivation

OVERLAP = {"anthropic": {"models": ["claude-sonnet-5-5"], "harnesses": ["claude", "hermes"]}}


def test_derive_weights_is_the_budget_share_per_provider():
    weights, reasoning = onboarding.derive_weights(OVERLAP, {"anthropic": {"claude": 3, "hermes": 1}})
    assert weights == {"anthropic": {"claude": 75.0, "hermes": 25.0}}
    assert reasoning == ["anthropic: claude $3/mo of $4 = 75%, hermes $1/mo of $4 = 25%; weights claude 75, hermes 25"]


def test_derive_weights_gives_no_weights_without_any_budget():
    weights, reasoning = onboarding.derive_weights(OVERLAP, {"anthropic": {"claude": 0, "hermes": 0}})
    assert weights == {} and "no monthly budget" in reasoning[0]


def test_budget_shares_sum_to_about_one_hundred_for_awkward_splits():
    overlap = {"anthropic": {"models": ["m"], "harnesses": ["claude", "hermes", "agy"]}}
    weights, _ = onboarding.derive_weights(overlap, {"anthropic": {"claude": 1, "hermes": 1, "agy": 1}})
    assert math.isclose(sum(weights["anthropic"].values()), 100, abs_tol=0.3)


def test_a_budget_above_the_cap_is_refused_and_nothing_is_written(h):
    answer_schema(h)
    code, out = h.office("onboard", "--harness-preference", "recommended", "--harness-budget", "anthropic:claude=1e308",
                         "--harness-budget", "anthropic:hermes=1e308", "--harness-budget", "anthropic:agy=0")
    assert code == 2 and "from zero to" in out, out
    assert "routing" not in h.user_data()


def test_hermes_without_a_usable_credential_does_not_join_the_overlap(h, monkeypatch):
    monkeypatch.setattr(hermes_access, "auth_status", lambda **_: {"anthropic": {"credentials": 1, "usable": 0, "exhausted": 1}})
    hp = h.status()["harness_preference"]
    assert [o["harnesses"] for o in hp["overlap"]] == [["agy", "claude"]]
    assert "hermes" not in hp["pending"]


def test_skip_with_a_harness_answer_is_refused_and_writes_nothing(h):
    code, out = h.office("onboard", "--skip", "--harness-preference", "keep")
    assert code == 2 and "takes no answers" in out, out
    assert not h.user.exists()


def test_answering_without_an_overlap_is_refused_and_writes_nothing(h):
    h.remove("hermes")
    h.remove("agy")
    code, out = h.office("onboard", "--harness-preference", "keep")
    assert code == 2 and "no installed harness shares a model" in out, out
    assert not h.user.exists()


def test_keep_leaves_existing_overlap_weights_untouched(h):
    weights = {"anthropic": {"claude": 60.0, "hermes": 40.0}}
    h.user.write_text(yaml.safe_dump({"routing": {"harness_preference": weights}}))
    code, out = h.office("onboard", "--harness-preference", "keep")
    assert code == 0, out
    assert h.user_data()["routing"]["harness_preference"] == weights


def test_recommended_keeps_other_provider_scopes(h):
    h.user.write_text(yaml.safe_dump({"routing": {"harness_preference": {"openai": {"codex": 100}}}}))
    code, out = h.office("onboard", "--harness-preference", "recommended", "--harness-budget", "anthropic:claude=60",
                         "--harness-budget", "anthropic:hermes=40", "--harness-budget", "anthropic:agy=0")
    assert code == 0, out
    assert h.user_data()["routing"]["harness_preference"] == {
        "openai": {"codex": 100}, "anthropic": {"claude": 60.0, "hermes": 40.0, "agy": 0.0}}


def test_one_model_under_two_providers_keeps_each_provider_its_own_harnesses(h, monkeypatch):
    real_rows = candidates.catalog_rows
    stray = {"invocation_harness": "claude", "model_id": "claude-sonnet-5-5", "lineage_key": "zz/claude-sonnet-5-5"}
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [stray] + real_rows())  # first, so it would win a model-only key
    hp = pref(h)
    assert [o["provider"] for o in hp["overlap"]] == ["anthropic"]
    assert hp["overlap"][0]["harnesses"] == OVERLAP_HARNESSES
