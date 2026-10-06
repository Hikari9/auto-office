"""The activity window is bounded, newest first, paginated and redacted."""
from __future__ import annotations

from office import db
from office.web import activity, synthetic


def _events(writer, n, **kwargs):
    path, con = writer
    with db.transaction(con):
        synthetic.insert_run(con, "R", git_common_dir="/src/r/.git")
        synthetic.insert_run(con, "OTHER", git_common_dir="/src/o/.git")
        synthetic.insert_event(con, "OTHER", "note", "not mine")
        for i in range(n):
            synthetic.insert_event(con, "R", "note", f"e{i}", **kwargs)
    return path


def test_default_max_and_newest_first(writer, make_observer):
    obs = make_observer(_events(writer, 260))
    page = obs.activity("R")
    assert len(page["items"]) == 50
    seqs = [i["seq"] for i in page["items"]]
    assert seqs == sorted(seqs, reverse=True)
    assert page["items"][0]["summary"] == "e259"
    assert len(obs.activity("R", limit=10_000)["items"]) == 200
    assert len(obs.activity("R", limit=0)["items"]) == 1


def test_before_seq_pages_through_everything_once(writer, make_observer):
    obs = make_observer(_events(writer, 23))
    seen, before = [], None
    while True:
        page = obs.activity("R", limit=5, before_seq=before)
        seen += [i["summary"] for i in page["items"]]
        before = page["next_before_seq"]
        if before is None:
            break
    assert seen == [f"e{i}" for i in reversed(range(23))]


def test_summary_and_payload_redaction(writer, make_observer, tmp_path):
    home = "/Users/someone"
    path, con = writer
    with db.transaction(con):
        synthetic.insert_run(con, "R", git_common_dir="/src/r/.git")
        synthetic.insert_event(
            con, "R", "note",
            f"GH_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123 Authorization: Bearer abc.def.ghi wrote {home}/Git/x.py "
            f"export OPENAI_API_KEY=\"sk-live\" from https://bob:hunter2@example.com/repo",
            payload={"token": "s3cret", "headers": {"Authorization": "Bearer zzz"}, "path": f"{home}/notes",
                     "nested": [{"api_key": "k"}, "sk-ant-abcdefghijklmnopqrstuvwx"], "count": 3,
                     "other_home": "/Users/someone-else/x"})
        synthetic.insert_event(con, "R", "note", "x" * 5000, payload={f"blob{i}": "y" * 900 for i in range(10)})
    obs = make_observer(path, home=home)
    big, item = obs.activity("R")["items"]
    text = item["summary"]
    for secret in ("ghp_abc", "abc.def.ghi", "sk-live", "hunter2", home):
        assert secret not in text, secret
    assert "GH_TOKEN=***" in text and "~/Git/x.py" in text
    p = item["payload"]
    assert p["token"] == "***" and p["headers"] == {"Authorization": "***"} and p["nested"][0]["api_key"] == "***"
    assert p["nested"][1] == "***" and p["path"] == "~/notes" and p["count"] == 3
    assert p["other_home"] == "/Users/someone-else/x"
    assert len(big["summary"]) < 600
    assert big["payload"]["truncated"] is True and len(big["payload"]["preview"]) == activity.PAYLOAD_CAP


def test_redact_text_helpers():
    assert activity.redact_text("password=abc123", home="/h") == "password=***"
    assert activity.shorten_home("/h/a and /hb", "/h") == "~/a and /hb"
    assert activity.redact_payload("not json AKIAABCDEFGHIJKLMNOP", home="/h") == "not json ***"
