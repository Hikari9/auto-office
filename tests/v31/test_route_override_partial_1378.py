"""#1378: `office route override` accepts a route without an effort, and the effort is inferred from the task size.

A receipt for `harness/model`, a bare harness or a model family records `effort_source: size` and matches only the
effort size inference picks for the dispatched task, never another one. `--as harness/model` on a sized task takes
that effort too and records that it was inferred. No model is called: fake harnesses and isolated homes only.
"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PLAN_ONE
from test_adaptive_dispatch import _approve, _quota
from office import candidates, db, route_override, routecmd
from office.state import Refused, Usage

HYDROGEN = "agy/gemini-4-hydrogen"
QUOTE = "Run hydrogen for this task, pick the effort from its size"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("AUTO_OFFICE_RUNS_DB", str(tmp_path / "runs.db"))
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    con = db.connect()
    db.migrate(con)
    con.execute("INSERT OR IGNORE INTO runs(id, status, repo_root) VALUES('run-aaaa1111','active',?)", (str(repo),))
    yield con
    con.close()


def manila(delta=timedelta(minutes=-5)):
    return (datetime.now(timezone.utc) + delta).astimezone(route_override.MANILA).strftime("%Y-%m-%d %H:%M")


def quoted(con, route=HYDROGEN, role="executor", **kw):
    return route_override.issue(con, run_id="run-aaaa1111", role=role, route=route, reason="Rico asked for it",
                                ttl_s=600, quote=QUOTE, quoted_at=manila(), **kw)


def cand(effort, harness="agy", model="gemini-4-hydrogen"):
    return {"harness": harness, "model_id": model, "effort": effort}


def found(con, candidate, size, role="executor"):
    return route_override.find_valid("run-aaaa1111", role, candidate, con=con, size=size)


def test_partial_receipt_stores_no_effort_and_matches_only_the_size_inferred_effort(home):
    receipt = quoted(home)
    assert (receipt["route"], receipt["effort_source"]) == (HYDROGEN, "size")
    stored = route_override.list_receipts(home, "run-aaaa1111")[0]
    assert (stored["route"], stored["effort_source"]) == (HYDROGEN, "size")
    assert found(home, cand("medium"), "S")["id"] == receipt["id"]       # S rung: hydrogen@medium
    assert found(home, cand("high"), "M")["id"] == receipt["id"]         # M rung: hydrogen@high
    assert found(home, cand("medium"), "L")["id"] == receipt["id"]       # no L rung: lowest effort meeting the L floor
    for effort in ("low", "high", "xhigh"):                                # never another effort at S
        assert found(home, cand(effort), "S") is None, effort
    assert found(home, cand("medium"), "M") is None
    for size in (None, "unsized"):                                         # an unsized task infers nothing
        assert found(home, cand("medium"), size) is None
    assert found(home, cand("medium"), "XL") is None                       # XL: no rung, no catalog effort fits
    assert found(home, cand("medium", harness="codex"), "S") is None       # another harness never matches


def test_harness_only_and_model_family_resolve_to_the_size_rung(home):
    agy = quoted(home, route="agy")
    assert (agy["route"], agy["effort_source"]) == ("agy/*", "size")
    assert found(home, cand("medium"), "S")["id"] == agy["id"]
    assert found(home, cand("high"), "S") is None
    assert found(home, cand("medium", model="claude-sonnet-5-5"), "S") is None  # not the S rung for agy
    route_override.revoke(home, agy["id"])
    family = quoted(home, route="gemini")
    assert (family["route"], family["effort_source"]) == ("*/gemini", "size")
    assert found(home, cand("medium"), "S")["id"] == family["id"]
    assert found(home, cand("high"), "S") is None
    route_override.revoke(home, family["id"])
    # `claude` is a harness first: the S rung for the claude harness is Haiku xhigh
    claude = quoted(home, route="claude")
    assert claude["route"] == "claude/*"
    assert found(home, cand("xhigh", harness="claude", model="claude-haiku-5-5"), "S")["id"] == claude["id"]
    assert found(home, cand("xhigh", harness="hermes", model="claude-haiku-5-5"), "S") is None


def test_an_ambiguous_or_missing_rung_is_refused_with_the_choices():
    with pytest.raises(Refused) as exc:
        candidates.infer_route("S", "executor", family="claude")  # Haiku xhigh on claude and on hermes
    assert exc.value.category == "effort-ambiguous"
    assert "claude/claude-haiku-5-5@xhigh" in exc.value.message and "hermes/claude-haiku-5-5@xhigh" in exc.value.message
    with pytest.raises(Refused) as exc:
        candidates.infer_route("XL", "executor", harness="hermes")
    assert exc.value.category == "effort-not-inferable" and "claude/claude-opus-5-5@high" in exc.value.message
    with pytest.raises(Refused) as exc:
        candidates.infer_route("S", "code_reviewer", harness="agy", model="gemini-4-hydrogen")
    assert exc.value.category == "effort-not-inferable"


@pytest.mark.parametrize("size, harness, model, effort", [
    ("S", "agy", "gemini-4-hydrogen", "medium"),    # S rung 3 (conditional) still gives its effort
    ("M", "agy", "gemini-4-hydrogen", "high"),
    ("L", "agy", "claude-sonnet-5-5", "high"),      # L rung 3
    ("S", "agy", "claude-sonnet-5-5", "medium"),    # no S rung: lowest catalog effort meeting the S floor
    ("S", "claude", "claude-haiku-5-5", "xhigh"),   # canary rung
    ("L", "codex", "gpt-6.1-terra", "high"),
    ("S", "codex", "gpt-6.1-terra", "medium"),
])
def test_infer_route_reads_the_rung_then_the_catalog_floor(size, harness, model, effort):
    assert candidates.infer_route(size, "executor", harness, model)["effort"] == effort


def test_a_row_without_its_own_score_reads_the_model_score_at_that_effort(monkeypatch):
    """pi rows carry no benchmark of their own: the same model's score at that effort on another harness counts."""
    index = candidates.executor_slates.POLICY["benchmark_index"]
    rows = [{"invocation_harness": "pi", "model_id": "claude-sonnet-5-5", "invocation_model_id": "anthropic/claude-sonnet-5-5",
             "effort": effort} for effort in ("medium", "high")]
    rows += [{"invocation_harness": "claude", "model_id": "claude-sonnet-5-5", "effort": "medium",
              "benchmark_indexes": {index: 37.3}},
             {"invocation_harness": "claude", "model_id": "claude-sonnet-5-5", "effort": "high",
              "benchmark_indexes": {index: 46.2}}]
    monkeypatch.setattr(candidates, "catalog_rows", lambda: rows)
    assert candidates.infer_route("L", "executor", "pi", "claude-sonnet-5-5")["effort"] == "high"
    assert candidates.infer_route("S", "executor", "pi", "claude-sonnet-5-5")["effort"] == "medium"
    with pytest.raises(Refused) as exc:
        candidates.infer_route("XL", "executor", "pi", "claude-sonnet-5-5")
    assert exc.value.category == "effort-not-inferable" and "@high (46.2)" in exc.value.message


def test_an_exact_triple_is_unchanged_and_keeps_the_old_mac_body(home):
    receipt = quoted(home, route="agy/gemini-4-hydrogen@high")
    assert (receipt["route"], receipt["effort_source"]) == ("agy/gemini-4-hydrogen@high", None)
    assert receipt["mac"] == route_override._mac({k: v for k, v in receipt.items() if k != "effort_source"})
    assert found(home, cand("high"), None)["id"] == receipt["id"]  # an exact route needs no size
    assert found(home, cand("medium"), "S") is None


def test_a_worker_is_refused_for_a_partial_route(home, monkeypatch):
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "D1")
    for route in (HYDROGEN, "agy", "gemini"):
        with pytest.raises(Refused) as exc:
            quoted(home, route=route)
        assert exc.value.category == "worker-cannot-override"
    assert route_override.list_receipts(home) == []


@pytest.mark.parametrize("route, role, code", [
    ("gemini-4-hydrogen", "executor", "invalid-route"),       # a model without its harness
    ("gemini-4-hydrogen@medium", "executor", "invalid-route"),
    ("agy/*", "executor", "invalid-route"),
    ("claude@2/claude-sonnet-5-5", "executor", "invalid-route"),
    ("nope/some-model", "executor", "unknown-harness"),
    (HYDROGEN, "code_reviewer", "invalid-route"),             # only executor and worker routes are sized
])
def test_malformed_partial_routes_are_refused(home, route, role, code):
    with pytest.raises(Usage) as exc:
        quoted(home, route=route, role=role)
    assert exc.value.category == code
    assert route_override.list_receipts(home) == []


def test_the_mac_covers_effort_source(home):
    receipt = quoted(home)
    assert found(home, cand("medium"), "S")
    home.execute("UPDATE route_override_receipts SET effort_source=NULL WHERE id=?", (receipt["id"],))
    assert found(home, cand("medium"), "S") is None
    home.execute("UPDATE route_override_receipts SET effort_source='size', route='agy/gemini-4-hydrogen@high' "
                 "WHERE id=?", (receipt["id"],))
    assert found(home, cand("high"), "S") is None


def test_an_old_receipt_without_effort_source_still_verifies_and_the_column_is_added(home):
    home.execute("DROP TABLE IF EXISTS route_override_receipts")
    home.execute("CREATE TABLE route_override_receipts(id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, "
                 "run_id TEXT NOT NULL, role TEXT NOT NULL, route TEXT NOT NULL, reason TEXT NOT NULL, issued_at TEXT "
                 "NOT NULL, expires_at TEXT NOT NULL, mac TEXT NOT NULL, revoked_at TEXT, quote TEXT, quoted_at TEXT, "
                 "issuer TEXT, pane TEXT)")
    old = {"id": "Oold0001", "nonce": "n" * 32, "run_id": "run-aaaa1111", "role": "executor",
           "route": "agy/gemini-4-hydrogen@medium", "reason": "old", "issued_at": "2026-11-20T00:00:00Z",
           "expires_at": "2999-01-01T00:00:00Z", "quote": QUOTE, "quoted_at": "2026-11-20 08:00",
           "issuer": "quoted", "pane": ""}
    old["mac"] = route_override._mac(old)
    home.execute("INSERT INTO route_override_receipts VALUES(:id,:nonce,:run_id,:role,:route,:reason,:issued_at,"
                 ":expires_at,:mac,NULL,:quote,:quoted_at,:issuer,:pane)", old)
    assert found(home, cand("medium"), None)["id"] == "Oold0001"
    assert route_override.list_receipts(home)[0]["effort_source"] is None


def test_the_cli_says_the_effort_will_be_inferred(home, monkeypatch):
    home.close()
    result = routecmd.override(route=HYDROGEN, role="executor", run="run-aaaa1111", reason="Rico asked", ttl=None,
                               rico_quote=QUOTE, quoted_at=manila())
    text = "\n".join(result.lines)
    assert "inferred from the task's size at dispatch" in text and "S agy/gemini-4-hydrogen@medium" in text
    assert result.data["receipt"]["effort_source"] == "size"


# --- dispatch: --as harness/model on a sized task, with and without a receipt for a denied route ---

def _sized_plan(size):
    return PLAN_ONE.replace("task_size: M", f"task_size: {size}")


def _dispatched(env):
    row = env.con().execute("SELECT triple, route_json FROM dispatches WHERE task_id='T1'").fetchone()
    return row[0], json.loads(row[1])


@pytest.mark.parametrize("size, effort", [("S", "medium"), ("L", "high")])
def test_as_without_effort_takes_the_size_effort_and_records_it_was_inferred(env, size, effort):
    _approve(env, plan=_sized_plan(size))
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6.1-terra", env=_quota(env))
    assert code == 0, out
    triple, route = _dispatched(env)
    assert triple.endswith(f"gpt-6.1-terra@{effort}") and route["candidate"]["effort"] == effort
    assert route["effort_inferred"] == f"size {size}"
    assert route["route_authority"]["effort_inferred"] == f"size {size}"
    assert f"effort inferred from size {size}" in route["selection_reason"]


def test_as_with_an_effort_is_not_inferred(env):
    _approve(env, plan=_sized_plan("S"))
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6.1-terra@high", env=_quota(env))
    assert code == 0, out
    triple, route = _dispatched(env)
    assert triple.endswith("gpt-6.1-terra@high") and "effort_inferred" not in route


def test_a_partial_receipt_lifts_the_deny_only_at_the_inferred_effort(env):
    _approve(env, plan=_sized_plan("S"))
    routecmd.change("deny", role="executor", route="codex/gpt-6.1-terra@medium")
    routecmd.change("deny", role="executor", route="codex/gpt-6.1-terra@high")
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6.1-terra", env=_quota(env))
    assert code != 0 and "denied" in out.lower(), out
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    receipt = route_override.issue(con, run_id=run_id, role="executor", route="codex/gpt-6.1-terra",
                                   reason="Rico asked for terra", ttl_s=600, quote=QUOTE, quoted_at=manila())
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6.1-terra@high", env=_quota(env))
    assert code != 0, out  # the receipt never covers an effort the size did not pick
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6.1-terra", env=_quota(env))
    assert code == 0, out
    triple, route = _dispatched(env)
    assert triple.endswith("gpt-6.1-terra@medium") and route["effort_inferred"] == "size S"
    assert receipt["id"] in json.dumps(route)


def _probe_pass(env, spec):
    """A fresh exact conformance pass for this Hydrogen route, as `office doctor --probe-route` records it."""
    from office import route_probe
    from office.util import now_iso
    harness, rest = spec.split("/", 1)
    model, effort = rest.split("@")
    probe = candidates.declared_candidate(harness, model, effort)
    adapter = route_probe._adapter(probe)
    fp = route_probe.fingerprint(probe, adapter)
    con = env.con()
    con.execute(
        "INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, "
        "effort, result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (route_probe.key(probe, adapter), fp["harness"], fp["harness_version"], fp["adapter_hash"], fp["profile"],
         fp["invocation_model_id"], fp["effort"], "pass", None, "seeded pass", now_iso(), None, None, "att-1378"))
    con.commit()


def test_an_s_task_with_a_partial_hydrogen_receipt_dispatches_at_medium(env):
    _approve(env, plan=_sized_plan("S"), gear="S")
    _probe_pass(env, "agy/gemini-4-hydrogen@medium")
    routecmd.change("deny", role="executor", route="agy/gemini-4-hydrogen@medium")
    code, out = env.office("dispatch", "T1", "--as", HYDROGEN, env=_quota(env))
    assert code != 0 and "denied" in out.lower(), out
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    receipt = route_override.issue(con, run_id=run_id, role="executor", route=HYDROGEN, reason="Rico asked for hydrogen",
                                   ttl_s=600, quote=QUOTE, quoted_at=manila())
    assert receipt["effort_source"] == "size"
    code, out = env.office("dispatch", "T1", "--as", HYDROGEN, env=_quota(env))
    assert code == 0, out
    triple, route = _dispatched(env)
    assert triple.endswith("gemini-4-hydrogen@medium") and route["candidate"]["effort"] == "medium"
    assert route["effort_inferred"] == "size S" and receipt["id"] in json.dumps(route)
