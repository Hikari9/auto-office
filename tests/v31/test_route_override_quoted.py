"""Quoted issuer for `office route override` (2026-11-15 "Any non-worker, quoted"); builds on the #837 receipts."""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from office import db, route_override, routecmd
from office.state import Refused, Usage

ROUTE = "claude/claude-opus-5-5@high"


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
    yield tmp_path / "config.yaml", con
    con.close()


def deny_reviewer():
    routecmd.change("deny", role="code_reviewer", route=ROUTE)


def issue(con, **kw):
    args = dict(run_id="run-aaaa1111", role="code_reviewer", route=ROUTE, reason="Rico asked for it",
                ttl_s=600, confirm=lambda _s: True, terminal_check=lambda: None)
    args.update(kw)
    return route_override.issue(con, **args)


QUOTE = "I want you to be able to run office route override by yourself"


def manila(delta=timedelta(0)):
    return (datetime.now(timezone.utc) + delta).astimezone(route_override.MANILA).strftime("%Y-%m-%d %H:%M")


def quoted(con, **kw):
    args = dict(run_id="run-aaaa1111", role="code_reviewer", route=ROUTE, reason="Rico asked for it", ttl_s=600,
                quote=QUOTE, quoted_at=manila(-timedelta(minutes=5)))
    args.update(kw)
    return route_override.issue(con, **args)


def test_quoted_path_issues_a_receipt_without_a_terminal(home, monkeypatch):
    _, con = home
    monkeypatch.setenv("HERDR_PANE_ID", "wEF:p9")
    receipt = quoted(con)  # no confirm, no terminal_check: pytest has no TTY
    assert receipt["issuer"] == "quoted" and receipt["pane"] == "wEF:p9" and receipt["quote"] == QUOTE
    deny_reviewer()
    stored = route_override.list_receipts(con, "run-aaaa1111")[0]
    assert (stored["issuer"], stored["quote"], stored["quoted_at"], stored["pane"]) == (
        "quoted", QUOTE, receipt["quoted_at"], "wEF:p9")
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", {
        "harness": "claude", "model_id": "claude-opus-5-5", "effort": "high"}, con=con)["id"] == receipt["id"]


def test_worker_is_refused_on_the_quoted_path(home, monkeypatch):
    _, con = home
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "D1")
    with pytest.raises(Refused) as exc:
        quoted(con)
    assert exc.value.category == "worker-cannot-override"
    assert route_override.list_receipts(con) == []


@pytest.mark.parametrize("kw", [{"quote": ""}, {"quote": "   "}, {"quote": "ok"}, {"quote": "<verbatim words>"},
                                {"quote": None}, {"quoted_at": None}, {"quote": "two words"}])
def test_missing_or_trivial_quote_is_refused(home, kw):
    _, con = home
    with pytest.raises(Usage) as exc:
        quoted(con, **kw)
    assert exc.value.category in {"quote-required", "quote-too-short"}
    assert route_override.list_receipts(con) == []


def test_future_stale_and_malformed_times_are_refused(home):
    _, con = home
    with pytest.raises(Usage) as exc:
        quoted(con, quoted_at=manila(timedelta(minutes=10)))
    assert exc.value.category == "quoted-at-future"
    with pytest.raises(Refused) as exc:
        quoted(con, quoted_at=manila(-timedelta(hours=25)))
    assert exc.value.category == "quote-stale"
    with pytest.raises(Usage) as exc:
        quoted(con, quoted_at="yesterday")
    assert exc.value.category == "invalid-quoted-at"
    assert route_override.list_receipts(con) == []
    assert quoted(con, quoted_at=manila(-timedelta(hours=23)))["issuer"] == "quoted"


def test_quoted_path_keeps_the_other_checks(home):
    _, con = home
    with pytest.raises(Usage) as exc:
        quoted(con, role="not-a-role")
    assert exc.value.category == "unknown-role"
    with pytest.raises(Usage) as exc:
        quoted(con, reason="")
    assert exc.value.category == "reason-required"
    with pytest.raises(Usage) as exc:
        quoted(con, route="claude/claude-opus-5-5")
    assert exc.value.category == "invalid-route"
    with pytest.raises(Usage) as exc:
        quoted(con, run_id="nope")
    assert exc.value.category == "unknown-run"
    with pytest.raises(Usage):
        route_override.parse_ttl("48h")


def test_terminal_path_is_unchanged_and_recorded_as_terminal(home):
    _, con = home
    with pytest.raises(Refused) as exc:  # still refused without a TTY and without a quote
        route_override.issue(con, run_id="run-aaaa1111", role="code_reviewer", route=ROUTE, reason="because", ttl_s=600)
    assert exc.value.category == "user-terminal-required"
    assert "--rico-quote" in str(exc.value.next_step)
    with pytest.raises(Refused) as exc:  # the prompt is still required
        issue(con, confirm=lambda _s: False)
    assert exc.value.category == "override-declined"
    receipt = issue(con)
    assert (receipt["issuer"], receipt["quote"], receipt["quoted_at"]) == ("terminal", "", "")


@pytest.mark.parametrize("field,value", [("quote", "different words entirely here"), ("quoted_at", "2020-01-01 00:00"),
                                         ("issuer", "terminal"), ("pane", "evil:p1")])
def test_mac_covers_the_new_fields(home, field, value):
    _, con = home
    receipt = quoted(con)
    cand = {"harness": "claude", "model_id": "claude-opus-5-5", "effort": "high"}
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con)
    con.execute(f"UPDATE route_override_receipts SET {field}=? WHERE id=?", (value, receipt["id"]))
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con) is None
    con.execute("UPDATE route_override_receipts SET issuer=NULL, quote=NULL, quoted_at=NULL, pane=NULL WHERE id=?",
                (receipt["id"],))  # stripping the fields to reach the legacy body does not verify either
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con) is None


def test_legacy_receipt_without_issuer_still_verifies_and_old_table_is_migrated(home):
    _, con = home
    con.execute("DROP TABLE IF EXISTS route_override_receipts")
    con.execute("CREATE TABLE route_override_receipts(id TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL, "
                "role TEXT NOT NULL, route TEXT NOT NULL, reason TEXT NOT NULL, issued_at TEXT NOT NULL, "
                "expires_at TEXT NOT NULL, mac TEXT NOT NULL, revoked_at TEXT)")
    legacy = {"id": "Olegacy1", "nonce": "n" * 32, "run_id": "run-aaaa1111", "role": "code_reviewer", "route": ROUTE,
              "reason": "old", "issued_at": "2026-11-15T00:00:00Z", "expires_at": "2999-01-01T00:00:00Z"}
    legacy["mac"] = route_override._mac(legacy)
    con.execute("INSERT INTO route_override_receipts VALUES(:id,:nonce,:run_id,:role,:route,:reason,:issued_at,:expires_at,"
                ":mac,NULL)", legacy)
    cand = {"harness": "claude", "model_id": "claude-opus-5-5", "effort": "high"}
    assert route_override.find_valid("run-aaaa1111", "code_reviewer", cand, con=con)["id"] == "Olegacy1"
    assert route_override.list_receipts(con)[0]["issuer"] is None
    assert routecmd._issuer_note(route_override.list_receipts(con)[0]) == ""


def test_list_shows_issuer_quote_and_pane(home, monkeypatch):
    _, con = home
    monkeypatch.setenv("HERDR_PANE_ID", "wEF:p9")
    con.close()
    monkeypatch.setattr(route_override, "require_user_terminal", lambda: None)
    result = routecmd.override(route=ROUTE, role="code_reviewer", run="run-aaaa1111", reason="Rico asked", ttl=None,
                               rico_quote=QUOTE, quoted_at=manila(-timedelta(minutes=1)))
    assert any("report this override" in line for line in result.lines)
    listed = routecmd.override(route=None, role=None, run=None, reason=None, ttl=None, list_receipts=True)
    assert "quoted" in listed.lines[0] and "wEF:p9" in listed.lines[0] and "office route override by yourself" in listed.lines[0]
    assert listed.data["receipts"][0]["issuer"] == "quoted" and "mac" not in listed.data["receipts"][0]
