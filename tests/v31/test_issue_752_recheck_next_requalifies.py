"""#752: the repair `next:` requalifies the task's recorded route as the rerun will, so a route the rerun
would refuse for quota is never offered as `office rerun T --resume|--fresh`."""
from test_convergence_contract import APPROVED, GOOD_ADD, GOOD_ADD2, _start, finding
from test_adaptive_dispatch import _quota


def _approved_with_a_finding(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED.replace("NEXT", finding("F1", severity="high", blocking=False)
                                                            + "\nNEXT fix concurrently")}])
    env.office("dispatch", "T1", check=0)


def test_the_repair_next_names_a_route_choice_when_quota_would_refuse_the_rerun(env):
    _approved_with_a_finding(env)
    code, out = env.office("disposition", "L-T1:F1", "fix", env=_quota(env, claude=1, codex=1))
    assert code == 0, out
    nxt = out.splitlines()[-1]
    assert "cannot run now" in nxt and "quota" in nxt, nxt
    assert 'office rerun T1 --fresh --as <harness>/<model>[@effort] --quote "<user\'s words>"' in nxt, nxt
    assert "office dispatch T1 --reroute" in nxt and "--resume" not in nxt, nxt


def test_the_repair_next_keeps_the_plain_rerun_when_the_route_still_qualifies(env):
    _approved_with_a_finding(env)
    code, out = env.office("disposition", "L-T1:F1", "fix", env=_quota(env))
    assert code == 0, out
    assert out.splitlines()[-1] == "next: office rerun T1 --resume|--fresh", out
