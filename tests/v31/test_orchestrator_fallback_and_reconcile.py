"""Run 6e0b0bac (#384): a reviewer that never replies falls through to the next
route, the orchestrator may review on behalf of a reviewer that returned no
verdict, and a submitted task whose gates all ended is accepted on resume."""
from test_convergence_contract import APPROVED, GOOD_ADD, _gates, _q, _scope, _start, _status


def test_empty_reply_after_reprompts_falls_through_to_the_next_route(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": ""}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["verdict"] == "APPROVED" and gate["round"] == 1 and gate["env_failures"] == 1, gate["summary"]
    assert _scope(env, "L-T1")["status"] == "approved"


def test_invalid_result_lets_the_orchestrator_review_on_the_reviewers_behalf(env, tmp_path):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": "looks fine to me"}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "convergence_review")[0]["review_status"] == "INVALID_RESULT"
    st = _scope(env, "L-T1")
    assert st["status"] == "attention" and st["fallback_available"]
    nxt = _status(env)["next"]
    assert "office review L-T1:convergence" in nxt and "authorized" in nxt, nxt
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    env.office("review", "L-T1:convergence", "--report", str(report), check=0)
    assert _gates(env, "convergence_review")[-1]["independence"] == "independent-orchestrator"
    assert _scope(env, "L-T1")["status"] == "approved"


def test_resume_accepts_a_submitted_task_whose_gates_all_ended(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    # The shape run 6e0b0bac was left in: checks APPROVED on the current revision,
    # but acceptance was evaluated while something held it, and nothing re-ran it.
    con = env.con()
    try:
        con.execute("UPDATE tasks SET status='submitted', accepted_revision_id=NULL WHERE id='T1'")
        con.commit()
    finally:
        con.close()
    env.office("resume", check=0)
    assert _q(env, "SELECT status FROM tasks WHERE id='T1'")[0]["status"] == "accepted"


def test_a_headless_reply_read_from_the_log_is_whole_and_never_pane_text(tmp_path):
    from office import gates
    review = "FINDING: " + "x" * 6000 + "\nVERDICT: APPROVED\n"
    (tmp_path / "output.log").write_text(review)
    (tmp_path / "pane-tail.txt").write_text("rate limit reached\n")
    d = {"id": "D1", "launcher": "process"}
    assert gates._reply_text(d, tmp_path, tmp_path / "reply.txt") == review
    assert gates._log_text(d, tmp_path) == review
    assert gates._log_text(d, tmp_path, pane=True).endswith("rate limit reached\n")
