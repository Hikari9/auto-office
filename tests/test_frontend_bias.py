"""AA v5 frontend bias: a frontend-heavy task moves its size's agy gemini-* rung to rung 1 for that task only.

Pure unit tier over executor_slates plus one routing.route decision. The bias reorders routes already on the slate:
it adds no candidate and lifts no gate."""
import pytest

from office import executor_slates as slates, routing

FRONTEND = ["src/app/Button.tsx", "src/app/Card.jsx", "src/app/Nav.vue", "src/app/Panel.svelte", "src/app/a.css",
            "src/app/b.scss", "src/app/c.sass", "src/app/d.less", "src/app/index.html", "src/app/Hero.astro"]


def _paths(frontend: int, total: int) -> list[str]:
    return [f"web/f{i}.tsx" for i in range(frontend)] + [f"src/office/m{i}.py" for i in range(total - frontend)]


def test_listed_extensions_are_exactly_the_frontend_set():
    assert slates.FRONTEND_EXTENSIONS == (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".sass", ".less",
                                          ".html", ".astro")
    assert slates.frontend_reason({"domain": "mixed"}, FRONTEND)
    assert slates.frontend_reason({}, ["src/app/Button.TSX"]), "extensions match case-insensitively"
    assert slates.frontend_reason({}, ["src/app/main.ts", "src/app/util.js", "docs/readme.md"]) is None


@pytest.mark.parametrize("frontend, total, heavy", [
    (59, 100, False),   # 59% is below the line
    (60, 100, True),    # 60% is on it
    (3, 5, True),       # 60% exactly
    (2, 5, False),      # 40%
    (1, 1, True),
    (0, 4, False),
])
def test_sixty_percent_path_boundary(frontend, total, heavy):
    assert bool(slates.frontend_reason({"domain": "mixed"}, _paths(frontend, total))) is heavy


@pytest.mark.parametrize("domain", ["mixed", "backend", None])
def test_non_ui_domains_are_judged_on_paths_alone(domain):
    assert slates.frontend_reason({"domain": domain}, _paths(6, 10))
    assert slates.frontend_reason({"domain": domain}, _paths(5, 10)) is None


def test_ui_domain_is_frontend_heavy_whatever_the_paths():
    assert slates.frontend_reason({"domain": "ui"}, ["src/office/m.py"]) == "descriptor domain is ui"
    assert slates.frontend_reason({"domain": "ui"}, None)


def test_shared_marker_blank_and_duplicate_paths_do_not_skew_the_ratio():
    scope = ["+web/a.tsx", "web/a.tsx", "", "  ", "src/office/m.py", "+src/office/n.py"]
    assert slates.frontend_reason({}, scope) is None  # 1 frontend of 3 distinct paths
    assert slates.frontend_reason({}, []) is None
    assert slates.frontend_reason({}, None) is None


@pytest.mark.parametrize("size, first, order", [
    ("XS", ("agy", "gemini-4-hydrogen", "low"),
     [("agy", "gemini-4-hydrogen", "low"), ("claude", "claude-haiku-5-5", "high"), ("codex", "gpt-6.1-terra", "low")]),
    ("S", ("agy", "gemini-4-hydrogen", "medium"),
     [("agy", "gemini-4-hydrogen", "medium"), ("claude", "claude-sonnet-5-5", "medium"),
      ("codex", "gpt-6.1-terra", "medium")]),
    ("M", ("agy", "gemini-4-hydrogen", "high"),
     [("agy", "gemini-4-hydrogen", "high"), ("claude", "claude-sonnet-5-5", "high"), ("codex", "gpt-6.1-terra", "high")]),
    ("XL", ("agy", "gemini-4-neon", "high"),
     [("agy", "gemini-4-neon", "high"), ("claude", "claude-opus-5-5", "high"), ("codex", "gpt-6.2-sol", "high")]),
])
def test_bias_moves_the_gemini_rung_to_rung_one_and_adds_no_route(size, first, order):
    seed = slates.ideal_seed(size)
    biased = slates.frontend_bias(seed, {"domain": "ui"}, [])
    assert [(r["harness"], r["model_id"], r["effort"]) for r in biased] == order
    assert (biased[0]["harness"], biased[0]["model_id"], biased[0]["effort"]) == first
    assert [r["rung"] for r in biased] == [1, 2, 3]
    assert biased[0]["frontend_bias"] == "descriptor domain is ui"
    assert sorted(map(str, ((r["harness"], r["model_id"], r["effort"]) for r in biased))) == sorted(
        map(str, ((r["harness"], r["model_id"], r["effort"]) for r in seed))), "no route added or removed"
    assert all("frontend_bias" not in r for r in biased[1:])


def test_bias_leaves_a_non_frontend_task_and_the_shared_slate_alone():
    seed = slates.ideal_seed("S")
    snapshot = [dict(r) for r in seed]
    assert slates.frontend_bias(seed, {"domain": "backend"}, _paths(1, 4)) is None
    assert slates.frontend_bias(seed, {"domain": "ui"}, []) is not None
    assert seed == snapshot, "the bias returns a new list: the shipped slate is never mutated"
    assert slates.ideal_seed("S") == snapshot, "the next task starts from the unbiased slate"


def test_bias_without_a_gemini_rung_does_nothing():
    seed = [{"model_id": "claude-sonnet-5-5", "effort": "high", "harness": "claude", "rung": 1},
            {"model_id": "gpt-6.1-terra", "effort": "high", "harness": "codex", "rung": 2}]
    assert slates.frontend_bias(seed, {"domain": "ui"}, []) is None
    assert slates.frontend_bias([], {"domain": "ui"}, []) is None


def test_the_disclosure_says_why_and_that_no_gate_moved():
    biased = slates.frontend_bias(slates.ideal_seed("XL"), {}, _paths(6, 10))
    chosen = {"model_id": "gemini-4-neon", "harness": "agy", "effort": "high", "quota": {"status": "ok"}}
    reason = routing.selection_disclosure("executor", chosen, biased, "balanced")["reason"]
    assert "frontend-heavy task (6 of 10 declared file paths are frontend files)" in reason
    assert "agy/gemini-4-neon@high moved to rung 1 for this task only" in reason
    assert "no route added, no gate lifted" in reason
    plain = routing.selection_disclosure("executor", chosen, slates.ideal_seed("XL"), "balanced")["reason"]
    assert "frontend-heavy" not in plain


class _Routed(Exception):
    pass


def _route_request(monkeypatch, tmp_path, scope, *, domain="mixed", size="L", user_seed=None):
    """The request route_role hands to routing.route for an executor task, captured at the routing seam."""
    from office import candidates, db, routing
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(candidates.state, "get_task", lambda *_a, **_k: {"scope": scope, "descriptor": {}})
    monkeypatch.setattr(candidates, "_effective_task_descriptor",
                        lambda *_a, **_k: {"task_size": size, "domain": domain})
    seen = {}

    def capture(request, **_kwargs):
        seen["request"] = request
        raise _Routed

    monkeypatch.setattr(routing, "route", capture)
    config = {"roles": {"executor": {"preferred_seed_by_size": {size: user_seed}}}} if user_seed else {}
    con = db.connect()
    try:
        with pytest.raises(_Routed):
            candidates.route_role(con, config, {"id": "r1", "gear": size, "risk": {"size_class": size}}, "executor",
                                  task_id="T1", probe=False)
    finally:
        con.close()
    return seen["request"]


def _first(request):
    row = request["preferred_seed"][0]
    return row["harness"], row["model_id"], row.get("frontend_bias")


def test_route_role_biases_a_frontend_heavy_task_and_leaves_a_backend_task_on_the_plain_slate(monkeypatch, tmp_path):
    # M keeps an agy gemini-* rung (conditional Hydrogen), so the bias applies there.
    heavy = _route_request(monkeypatch, tmp_path, ["web/a.tsx", "web/b.css", "src/office/m.py"], size="M")
    assert _first(heavy) == ("agy", "gemini-4-hydrogen", "2 of 3 declared file paths are frontend files")
    plain = _route_request(monkeypatch, tmp_path, ["web/a.tsx", "src/office/m.py", "src/office/n.py"], size="M")
    assert _first(plain) == ("claude", "claude-sonnet-5-5", None)
    assert [r["model_id"] for r in plain["preferred_seed"]] == ["claude-sonnet-5-5", "gemini-4-hydrogen", "gpt-6.1-terra"], (
        "the plain slate keeps the table order")


def test_route_role_has_no_frontend_bias_at_l_because_the_l_slate_has_no_gemini_rung(monkeypatch, tmp_path):
    # AA v5 scoreboard (#1356): L is Sonnet high on claude, hermes and agy, then Terra. No agy gemini-* rung to move.
    heavy = _route_request(monkeypatch, tmp_path, ["web/a.tsx", "web/b.css", "src/office/m.py"], size="L")
    assert _first(heavy) == ("claude", "claude-sonnet-5-5", None)
    assert all("frontend_bias" not in row for row in heavy["preferred_seed"])


def test_route_role_never_biases_a_users_own_seed(monkeypatch, tmp_path):
    seed = [{"harness": "claude", "model_id": "claude-opus-5-5", "effort": "medium"},
            {"harness": "agy", "model_id": "gemini-4-neon", "effort": "medium"}]
    request = _route_request(monkeypatch, tmp_path, ["web/a.tsx", "web/b.css"], size="L", user_seed=seed)
    assert all("frontend_bias" not in row for row in request["preferred_seed"])
    assert request["preferred_seed"][0]["model_id"] == "claude-opus-5-5"



def test_directories_and_test_paths_are_not_declared_work_files():
    # Two of three real files are frontend; the directory and the test paths must not dilute the ratio.
    scope = ["web/a.tsx", "web/b.html", "src/office/m.py", "web/components/", "tests/web/", "tests/test_button.py",
             "web/Button.test.tsx", "web/__tests__/card.spec.tsx", "docs/**"]
    assert slates.frontend_reason({}, scope) == "2 of 3 declared file paths are frontend files"
    # Only directories and tests declared: nothing to judge, so no bias.
    assert slates.frontend_reason({}, ["web/components/", "tests/web/", "web/Button.test.tsx"]) is None
    # A backend task whose only frontend entry is a template file stays below the line once directories drop out.
    assert slates.frontend_reason({}, ["app/templates/a.html", "app.py", "views.py", "app/static/"]) is None


def test_html_counts_as_a_frontend_file():
    assert slates.frontend_reason({}, ["app/templates/a.html", "app/templates/b.html", "app.py"])
