"""#1282: Hydrogen's executor gate names its real refusal cause and accepts a user's explicit --as at any effort.

Regression of runs 830b846e and 22f17fe3: an S task routed with --as agy/gemini-4-hydrogen@high and a fresh exact probe
pass was refused with the generic hydrogen-manual-unqualified + "run a probe". The real routes, adapters and catalog
run here; only the installed-harness probes and the run/task size are stubbed. No model is called.
"""
import pytest

from office import adapters, candidates, db, hydrogen_trials as h, route_probe
from office.state import Refused
from office.util import now_iso

HIGH = "agy/gemini-4-hydrogen@high"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [])
    monkeypatch.setattr(adapters, "installed", lambda _a: True)
    monkeypatch.setattr(adapters, "route_version", lambda *_a: "1.2.3")
    return tmp_path


def _size(monkeypatch, size):
    """The run and task classify this size; None leaves both unclassified."""
    monkeypatch.setattr(candidates.state, "get_run", lambda *_: {"risk": {"size_class": size}} if size else {})
    monkeypatch.setattr(candidates.state, "get_task",
                        lambda *_: {"descriptor": {"task_size": size}} if size else {})


def _probe_pass(spec):
    cand = candidates.declared_candidate(*spec.replace("@", "/").split("/")[0:1], "gemini-4-hydrogen",
                                         spec.rsplit("@", 1)[1])
    adapter = route_probe._adapter(cand)
    fp = route_probe.fingerprint(cand, adapter)
    with db.connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
            "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (route_probe.key(cand, adapter), fp["harness"], fp["harness_version"], fp["adapter_hash"], fp["profile"],
             fp["invocation_model_id"], fp["effort"], "pass", None, "seeded pass", now_iso(), "run-1282", None, "att-1282"))


def _decide(spec=HIGH, *, run_id="r1", scope="T1"):
    return candidates.declared_decision(spec, role="executor", config={}, run_id=run_id, scope=scope)


@pytest.mark.parametrize("size", ["XS", "S", "M", "L"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_explicit_as_at_any_effort_dispatches_for_xs_to_l_after_a_probe_pass(env, monkeypatch, size, effort):
    """830b846e/22f17fe3: S task, --as @high, probe pass is accepted, and so is every other XS-L effort."""
    _size(monkeypatch, size)
    spec = f"agy/gemini-4-hydrogen@{effort}"
    _probe_pass(spec)
    assert _decide(spec)


@pytest.mark.parametrize("size, spec, code, words, next_step", [
    ("XL", HIGH, "hydrogen-xl-excluded", "excluded for XL", "Opus, Sol or Neon"),
    (None, HIGH, "hydrogen-no-task-size", "run and task that classify its size", "name the run and task"),
    ("S", HIGH, "hydrogen-no-probe", "conformance receipt", "office doctor --probe-route agy/gemini-4-hydrogen@high"),
])
def test_each_refusal_names_its_real_cause_and_next_step(env, monkeypatch, size, spec, code, words, next_step):
    _size(monkeypatch, size)
    with pytest.raises(Refused) as refusal:
        _decide(spec, scope="T1" if size else None)
    assert refusal.value.category == code
    assert words in refusal.value.message
    assert next_step in refusal.value.next_step
    assert refusal.value.category != "hydrogen-manual-unqualified"


def test_xl_is_refused_even_with_a_pass(env, monkeypatch):
    _size(monkeypatch, "XL")
    _probe_pass(HIGH)
    with pytest.raises(Refused) as refusal:
        _decide()
    assert refusal.value.category == "hydrogen-xl-excluded"


def test_missing_probe_is_not_reported_as_a_size_or_effort_problem(env, monkeypatch):
    _size(monkeypatch, "M")
    with pytest.raises(Refused) as refusal:
        _decide("agy/gemini-4-hydrogen@low")
    assert refusal.value.category == "hydrogen-no-probe"
    assert "gemini-4-hydrogen@low" in refusal.value.next_step


@pytest.mark.parametrize("size, effort", [("XS", "low"), ("S", "medium"), ("M", "high")])
def test_automatic_effort_per_size(size, effort):
    assert h.exact_effort(size) == effort


@pytest.mark.parametrize("size", ["L", "XL", None])
def test_no_automatic_effort_for_l_xl_or_unsized(size):
    assert h.exact_effort(size) is None


def _cand(effort):
    return {"harness": "agy", "model_id": h.MODEL, "effort": effort}


@pytest.mark.parametrize("size, effort, code", [
    ("S", "high", "hydrogen-effort-mismatch"),
    ("XS", "medium", "hydrogen-effort-mismatch"),
    ("M", "low", "hydrogen-effort-mismatch"),
    ("L", "high", "hydrogen-effort-mismatch"),
    ("XL", "high", "hydrogen-xl-excluded"),
    (None, "high", "hydrogen-no-task-size"),
])
def test_automatic_routing_refusals_are_distinct(size, effort, code):
    config = {"routing": {"hydrogen_trials": {"enabled": True}}}
    refusal = h.executor_refusal(_cand(effort), size=size, manual=False, config=config)
    assert refusal[0] == code
    assert refusal[2], "every refusal carries a next step"


def test_manual_and_automatic_agree_only_on_the_user_visible_size_rules():
    config = {"routing": {"hydrogen_trials": {"enabled": True}}}
    assert h.executor_refusal(_cand("high"), size="S", manual=True, config=config) is None
    assert h.executor_refusal(_cand("high"), size="L", manual=True, config=config) is None
    assert h.executor_refusal(_cand("high"), size="XL", manual=True, config=config)[0] == "hydrogen-xl-excluded"
    assert h.executor_refusal(_cand("high"), size=None, manual=True, config=config)[0] == "hydrogen-no-task-size"
    wrong = h.executor_refusal({**_cand("high"), "harness": "codex"}, size="S", manual=True, config=config)
    assert wrong[0] == "hydrogen-wrong-harness"


def test_visual_reviewer_hydrogen_runs_at_low_only_and_code_reviewer_is_not_a_hydrogen_role():
    assert h.REVIEW_EFFORTS == {"plan_reviewer": "high", "visual_reviewer": "low"}
    assert "code_reviewer" not in h.REVIEW_ROLES
    allowed = dict(role="visual_reviewer", size=None, manual=False, user_allowed=True)
    assert h.route_refusal(_cand("low"), **allowed) is None
    for effort in ("medium", "high"):
        assert "agy/gemini-4-hydrogen@low" in h.route_refusal(_cand(effort), **allowed)
    plan = dict(role="plan_reviewer", size=None, manual=False, user_allowed=True)
    assert h.route_refusal(_cand("high"), **plan) is None
    assert "agy/gemini-4-hydrogen@high" in h.route_refusal(_cand("low"), **plan)
    assert "code review left Hydrogen" in h.route_refusal(_cand("high"), role="code_reviewer", size=None,
                                                          manual=True, user_allowed=True)


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_an_explicit_as_keeps_the_requested_route_and_effort(env, monkeypatch, effort):
    _size(monkeypatch, "S")
    spec = f"agy/gemini-4-hydrogen@{effort}"
    _probe_pass(spec)
    decision = _decide(spec)
    assert decision["candidate"]["model_id"] == "gemini-4-hydrogen"
    assert decision["candidate"]["effort"] == effort and decision["candidate"]["harness"] == "agy"


def test_a_run_and_task_that_exist_but_carry_no_size_name_the_missing_size(env, monkeypatch):
    """The executor_refusal path (a task is found, its size is not), not the no-run/no-scope guard."""
    _size(monkeypatch, None)
    with pytest.raises(Refused) as refusal:
        _decide(scope="T1")
    assert refusal.value.category == "hydrogen-no-task-size"
    assert "has none" in refusal.value.message and "descriptor task_size" in refusal.value.next_step


SAFE_RISK = {"size_class": "S", "blast_radius": "local"}
SAFE_DESCRIPTOR = {"task_size": "S", "bounds": "bounded", "domain": "backend"}


@pytest.mark.parametrize("size, effort, kwargs, code, words", [
    ("L", "high", {}, "hydrogen-effort-mismatch", "no automatic effort for L"),
    ("L", "low", {"user_allowed": True}, "hydrogen-effort-mismatch", "automatically at @high for L"),
    ("XXL", "high", {}, "hydrogen-no-task-size", "does not know task size"),
    ("XS", "low", {}, "hydrogen-manual-required", "XS requires explicit manual selection"),
    ("S", "medium", {"risk": SAFE_RISK, "descriptor": SAFE_DESCRIPTOR, "disabled": True},
     "hydrogen-canary-disabled", "canary is disabled"),
    ("S", "medium", {"risk": {**SAFE_RISK, "irreversible": True}, "descriptor": SAFE_DESCRIPTOR},
     "hydrogen-task-not-safe", "bounded, reversible, low-risk"),
])
def test_every_automatic_refusal_has_its_own_code_and_next_step(size, effort, kwargs, code, words):
    kwargs = dict(kwargs)
    enabled = not kwargs.pop("disabled", False)
    config = {"routing": {"hydrogen_trials": {"enabled": enabled}}}
    refusal = h.executor_refusal(_cand(effort), size=size, manual=False, config=config, **kwargs)
    assert refusal and refusal[0] == code and words in refusal[1] and refusal[2]


def test_a_user_allow_at_l_high_and_a_safe_automatic_s_task_are_not_refused():
    config = {"routing": {"hydrogen_trials": {"enabled": True}}}
    assert h.executor_refusal(_cand("high"), size="L", manual=False, config=config, user_allowed=True) is None
    assert h.executor_refusal(_cand("medium"), size="S", manual=False, config=config, risk=SAFE_RISK,
                              descriptor=SAFE_DESCRIPTOR) is None


def test_manual_denial_refuses_a_review_role_for_its_own_cause_and_needs_a_receipt(env):
    with db.connect() as con:
        visual = h.manual_denial(con, _cand("low"), role="visual_reviewer", size=None, user_allowed=False)
        assert visual[0] == "hydrogen-role-refused" and "exact allow" in visual[1]
        code_review = h.manual_denial(con, _cand("high"), role="code_reviewer", size=None, user_allowed=True)
        assert code_review[0] == "hydrogen-role-refused" and "code review left Hydrogen" in code_review[1]
        # An allowed visual review and an XS-L executor both still need the fresh exact receipt.
        assert h.manual_denial(con, _cand("low"), role="visual_reviewer", size=None, user_allowed=True)[0] == "hydrogen-no-probe"
        assert h.manual_denial(con, _cand("high"), role="executor", size="S")[0] == "hydrogen-no-probe"
    no_db = h.manual_denial(None, _cand("high"), role="executor", size="S")
    assert no_db[0] == "hydrogen-no-probe" and "runs.db" in no_db[1]


def _ids(role, **kwargs):
    from office import routing
    with db.connect() as con:
        built, _skipped = candidates.build_candidates(con, role, probe=False, **kwargs)
    return {routing.candidate_id(c): c for c in built}


def test_the_vision_probe_lists_only_hydrogen_low_for_the_visual_reviewer_and_it_still_needs_the_allow(env):
    hydrogen = {i for i in _ids("visual_reviewer", vision_probe=True) if "gemini-4-hydrogen" in i}
    assert hydrogen == {"agy@1/gemini-4-hydrogen@low"}
    assert not [i for i in _ids("visual_reviewer") if "gemini-4-hydrogen" in i], "never listed without the probe flag"
    for role in ("plan_reviewer", "code_reviewer"):
        assert not [i for i in _ids(role, vision_probe=True) if "gemini-4-hydrogen" in i], role
    cand = _ids("visual_reviewer", vision_probe=True)["agy@1/gemini-4-hydrogen@low"]
    refusal = h.route_refusal(cand, role="visual_reviewer", size=None, manual=False, user_allowed=False)
    assert "needs the user's exact allow" in refusal, "the proof alone never makes the route dispatch"

