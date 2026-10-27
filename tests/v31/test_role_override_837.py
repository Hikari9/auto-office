"""#837: authenticated one-off user override, migration diff, strict allow-only, fail-closed matching."""
import subprocess
import sqlite3

import pytest
import yaml

from office import candidates, config, db, route_override, route_policy, routecmd, routing
from office.state import OfficeError, Refused, Usage

ROUTE = "claude/claude-opus-5-5@high"


@pytest.fixture
def home(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(cfg))
    monkeypatch.setenv("AUTO_OFFICE_RUNS_DB", str(tmp_path / "runs.db"))
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    con = db.connect()
    db.migrate(con)
    con.execute("INSERT OR IGNORE INTO runs(id, status, repo_root) VALUES('run-aaaa1111','active',?)", (str(repo),))
    yield cfg, con
    con.close()


def deny_reviewer():
    routecmd.change("deny", role="code_reviewer", route=ROUTE)


def issue(con, **kw):
    args = dict(run_id="run-aaaa1111", role="code_reviewer", route=ROUTE, reason="Rico asked for it",
                ttl_s=600, confirm=lambda _s: True, terminal_check=lambda: None)
    args.update(kw)
    return route_override.issue(con, **args)


def test_non_terminal_cannot_issue_a_receipt(home):
    _, con = home
    with pytest.raises(Refused) as exc:
        route_override.issue(con, run_id="run-aaaa1111", role="code_reviewer", route=ROUTE, reason="because", ttl_s=600)
    assert exc.value.category == "user-terminal-required"


def test_worker_env_cannot_issue_a_receipt(home, monkeypatch):
    _, con = home
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "D1")
    with pytest.raises(Refused) as exc:
        route_override.require_user_terminal()
    assert exc.value.category == "worker-cannot-override"


def test_declined_confirmation_issues_nothing(home):
    _, con = home
    with pytest.raises(Refused):
        issue(con, confirm=lambda _s: False)
    assert route_override.list_receipts(con) == []


def test_receipt_is_bound_to_run_role_route_and_expiry(home):
    _, con = home
    receipt = issue(con)
    cand = {"harness": "claude", "model_id": "claude-opus-5-5", "invocation_model_id": "claude-opus-5-5", "effort": "high"}
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con)["id"] == receipt["id"]
    assert route_override.find_valid("run-bbbb2222", "code_reviewer", cand, con=con) is None
    assert route_override.find_valid("run-aaaa1111", "executor", cand, con=con) is None
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", {**cand, "effort": "low"}, con=con) is None
    con.execute("UPDATE route_override_receipts SET expires_at='2000-01-01T00:00:00Z' WHERE id=?", (receipt["id"],))
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con) is None


def test_tampered_or_forged_receipt_is_ignored(home):
    _, con = home
    receipt = issue(con)
    cand = {"harness": "claude", "model_id": "claude-opus-5-5", "invocation_model_id": "claude-opus-5-5", "effort": "high"}
    con.execute("UPDATE route_override_receipts SET role='executor' WHERE id=?", (receipt["id"],))
    assert route_override.find_valid("run-aaaa1111", "executor", cand, con=con) is None
    # a row written directly, without the user's key, carries no valid MAC
    con.execute("INSERT INTO route_override_receipts(id,nonce,run_id,role,route,reason,issued_at,expires_at,mac) "
                "VALUES('Oforged','n','run-aaaa1111','code_reviewer',?, 'x','2026-01-01T00:00:00Z','2999-01-01T00:00:00Z','00')", (ROUTE,))
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con) is None


def test_revoked_receipt_stops_working(home):
    _, con = home
    receipt = issue(con)
    cand = {"harness": "claude", "model_id": "claude-opus-5-5", "invocation_model_id": "claude-opus-5-5", "effort": "high"}
    assert route_override.revoke(con, receipt["id"])
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con) is None


def test_ttl_bounds():
    assert route_override.parse_ttl("30m") == 1800
    for bad in ("5s", "48h", "x"):
        with pytest.raises(Usage):
            route_override.parse_ttl(bad)


def test_quote_or_pin_cannot_override_a_deny_but_a_receipt_can(home):
    _, con = home
    deny_reviewer()
    with pytest.raises(Refused) as exc:
        candidates.declared_decision(ROUTE, flag="--review-as", role="code_reviewer", run_id="run-aaaa1111")
    assert exc.value.category == "route-denied" and "office route override" in exc.value.next_step
    receipt = issue(con)
    decision = candidates.declared_decision(ROUTE, flag="--review-as", role="code_reviewer", run_id="run-aaaa1111")
    assert decision["candidate"]["policy_override"]["receipt"] == receipt["id"]
    used = con.execute("SELECT COUNT(*) FROM route_override_uses WHERE receipt_id=?", (receipt["id"],)).fetchone()[0]
    assert used == 1
    # the override is for this role only: an executor deny is not lifted by a reviewer receipt
    routecmd.change("deny", role="executor", route=ROUTE)
    with pytest.raises(Refused):
        candidates.declared_decision(ROUTE, flag="--as", role="executor", run_id="run-aaaa1111")


def test_override_does_not_bypass_required_capabilities(home):
    _, con = home
    deny_reviewer()
    issue(con)
    cfg = config.resolve(None)[0]
    cfg["roles"]["code_reviewer"]["required_capabilities"] = ["no-such-capability"]
    with pytest.raises(Refused) as exc:
        candidates.declared_decision(ROUTE, flag="--review-as", role="code_reviewer", config=cfg, run_id="run-aaaa1111")
    assert exc.value.category == "capability-missing"


def cand(model="claude-opus-5-5", effort="high", harness="claude", version="2.1.311"):
    return {"harness": harness, "harness_version": version, "model_id": model, "invocation_model_id": model, "effort": effort,
            "capabilities": ["review"], "quota": {"status": "ok", "tightest_remaining_percent": 80},
            "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1}, "route_status": "available"}


def rules(*items, strict=()):
    return {"role_rules": list(items), "denied": [], "overkill": [], "ceilings": {}, "strict_roles": list(strict),
            "sources": {"role_rules": "user"}}


def run_route(rows, *policies, overrides=None, strict=False, seed=None):
    request = {"role": "code_reviewer", "candidates": rows, "preferred_seed": seed,
               "policy": {"floor": None, "strict_seed": strict, "required_capabilities": ["review"]},
               "user_policies": list(policies), "runs_db": None}
    if overrides:
        request["policy_overrides"] = overrides
    if strict:
        request["strict_seed_user_authorized"] = True
    return routing.route(request)


def test_router_honours_only_a_granted_receipt_for_that_candidate():
    pol = rules({"role": "code_reviewer", "route": ROUTE, "state": "deny"})
    row = cand()
    assert run_route([row], pol)["status"] == "no_qualifying_candidate"
    ok = run_route([row], pol, overrides={routing.candidate_id(row): "O1"})
    assert ok["status"] == "selected"
    assert run_route([row], pol, overrides={"claude@2/other@high": "O1"})["status"] == "no_qualifying_candidate"


def test_conflicting_equivalent_rules_fail_closed():
    pol = rules({"role": "code_reviewer", "route": ROUTE, "state": "allow"},
                {"role": "code_reviewer", "route": "claude@2/claude-opus-5-5@high", "state": "deny"})
    state = route_policy.effective_role_state(cand(), "code_reviewer", [pol])
    assert state["state"] == "deny"


def test_deny_with_major_does_not_fail_open_on_an_unreadable_harness_version():
    pol = rules({"role": "code_reviewer", "route": "claude@2/claude-opus-5-5@high", "state": "deny"})
    assert route_policy.effective_role_state(cand(version=None), "code_reviewer", [pol])["state"] == "deny"
    allow = rules({"role": "code_reviewer", "route": "claude@2/claude-opus-5-5@high", "state": "allow"})
    assert route_policy.effective_role_state(cand(version=None), "code_reviewer", [allow])["source"] != "user"


def test_strict_allow_only_is_an_explicit_user_opt_in():
    seed = [{"model_id": "gpt-6-luna", "effort": "high"}]
    neutral = cand("deepseek-v4.1", "low")
    assert run_route([neutral], seed=seed, strict=False)["status"] == "selected"
    assert run_route([neutral], rules(strict=["code_reviewer"]), seed=seed, strict=True)["status"] != "selected"


def test_strict_allow_roles_validation_and_repo_cannot_set_it():
    problems = route_policy.validate_user_policy({"routing": {"user_policy": {"strict_allow_roles": ["nope"]}}})
    assert any("strict_allow_roles" in p for p in problems)
    with pytest.raises(ValueError, match="strict_allow_roles"):
        config.resolve(None, files={"user": None,
            "repo": "routing:\n  user_policy:\n    strict_allow_roles: [code_reviewer]\n"})


def test_migrate_dry_run_writes_nothing_then_apply_and_rollback(home):
    cfg, _ = home
    cfg.write_text(yaml.safe_dump({"routing": {"user_policy": {"denied_models": ["claude/opus@low", "harness:agy"]}}}))
    before = cfg.read_text()
    out = routecmd.migrate()
    assert out.data["pending"] == ["claude/opus@low", "harness:agy"] and cfg.read_text() == before
    out = routecmd.migrate(apply=True)
    saved = yaml.safe_load(cfg.read_text())["routing"]["user_policy"]
    assert saved["denied_models"] == ["claude/opus@low", "harness:agy"]  # nothing removed
    assert [r["state"] for r in saved["role_rules"]] == ["deny", "deny"] and all(r["role"] == "*" for r in saved["role_rules"])
    assert routecmd.migrate().data["pending"] == []  # idempotent
    routecmd.migrate(rollback=True)
    assert cfg.read_text() == before
