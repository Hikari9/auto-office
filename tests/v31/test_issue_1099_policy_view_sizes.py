"""#1099: the unsized `office route policy --effective` view names the states that differ by task size.

Unit tier. An isolated user config and a temp repository, the real catalog and candidate code, and a pinned
installed Claude major. No model is called.
"""
import subprocess

import pytest
import yaml

from office import adapters, routecmd

INSTALLED_CLAUDE = "2.1.311"
SONNET = "claude/claude-sonnet-5-5@high"
PLAIN = "codex/gpt-6-luna@low"  # no shipped executor rule, so no size scope
SHIPPED_SONNET_BY_SIZE = "deny XS,S,M (shipped), allow L (shipped), neutral XL (default)"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    target = tmp_path / "my-config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(target))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    real = adapters.route_version

    def route_version(harness, adapter, sources=None):
        return INSTALLED_CLAUDE if harness == "claude" else real(harness, adapter, sources)

    monkeypatch.setattr(adapters, "route_version", route_version)
    return target


def write_rules(target, *rules):
    target.write_text(yaml.safe_dump({"routing": {"user_policy": {"role_rules": list(rules)}}}))


def view(size=None):
    return routecmd.policy(role="executor", effective=True, size=size)


def line_for(result, route):
    [line] = [ln for ln in result.lines if ln.startswith(f"  {route}: ")]
    return line


def row_for(result, route):
    [row] = [r for r in result.data["effective"] if r["route"] == route]
    return row


def test_the_unsized_line_names_each_task_sizes_state_and_source(isolated):
    line = line_for(view(), SONNET)
    assert line.startswith(f"  {SONNET}: neutral (default) -> ")
    assert line.endswith(f"; by size: {SHIPPED_SONNET_BY_SIZE}")


def test_the_unsized_row_carries_by_size_as_a_map(isolated):
    row = row_for(view(), SONNET)
    assert row["state"] == "neutral" and row["source"] == "default"
    assert row["by_size"] == {
        "XS": {"state": "deny", "source": "shipped"},
        "S": {"state": "deny", "source": "shipped"},
        "M": {"state": "deny", "source": "shipped"},
        "L": {"state": "allow", "source": "shipped"},
        "XL": {"state": "neutral", "source": "default"},
    }


def test_a_user_size_rule_is_named_with_its_source_and_grouped_by_state(isolated):
    write_rules(isolated, {"role": "executor", "route": SONNET, "state": "allow", "sizes": ["XS"]})
    result = view()
    assert line_for(result, SONNET).endswith(
        "; by size: allow XS (user), deny S,M (shipped), allow L (shipped), neutral XL (default)")
    assert row_for(result, SONNET)["by_size"]["XS"] == {"state": "allow", "source": "user"}


def test_a_route_without_sized_rules_has_no_suffix_and_no_by_size_key(isolated):
    result = view()
    line = line_for(result, PLAIN)
    assert "by size" not in line
    assert "by_size" not in row_for(result, PLAIN)


def test_an_unsized_user_rule_covers_every_size_so_no_suffix_is_shown(isolated):
    write_rules(isolated, {"role": "executor", "route": PLAIN, "state": "allow"})
    result = view()
    assert "by size" not in line_for(result, PLAIN)
    assert "by_size" not in row_for(result, PLAIN)


def test_a_sized_view_shows_no_by_size_breakdown(isolated):
    result = view(size="M")
    assert not [ln for ln in result.lines if "by size" in ln]
    assert not [r for r in result.data["effective"] if "by_size" in r]
    assert row_for(result, SONNET)["state"] == "deny"


def test_a_route_change_says_running_runs_apply_the_rule_after_the_existing_lines(isolated):
    result = routecmd.change("allow", role="executor", route=PLAIN)
    assert result.lines[0].startswith("executor/") and result.lines[0].endswith(": allow (user config)")
    assert result.lines[-1] == ("running runs apply this rule at their next routing decision; "
                                "`office config --run <id> --apply-routing --quote \"<words>\"` "
                                "re-pins the rest of a run's pinned routing")


def test_a_route_change_that_writes_nothing_says_nothing_about_running_runs(isolated):
    routecmd.change("allow", role="executor", route=PLAIN)
    result = routecmd.change("allow", role="executor", route=PLAIN)
    assert result.data["changed"] is False
    assert len(result.lines) == 1


def test_a_preferred_seed_allow_is_named_by_size_and_a_user_size_deny_outranks_it(isolated):
    isolated.write_text(yaml.safe_dump({
        "roles": {"executor": {"preferred_seed": [{"harness": "codex", "model_id": "gpt-6-luna", "effort": "low"}]}},
        "routing": {"user_policy": {"role_rules": [{"role": "executor", "route": PLAIN, "state": "deny", "sizes": ["S"]}]}}}))
    result = view()
    assert line_for(result, PLAIN).endswith("; by size: allow XS,M,L,XL (preferred_seed), deny S (user)")
    assert row_for(result, PLAIN)["by_size"]["S"] == {"state": "deny", "source": "user"}


def test_an_empty_size_is_unknown_like_no_size_so_the_unsized_view_is_shown(isolated):
    unsized, empty = view(), view(size="")
    assert empty.lines == unsized.lines
    assert line_for(empty, SONNET).endswith(f"; by size: {SHIPPED_SONNET_BY_SIZE}")
    assert row_for(empty, SONNET)["by_size"] == row_for(unsized, SONNET)["by_size"]
