"""#1268: Claude reviewers fell back to reply.txt because the brief offered `office review submit` "on stdin, or
--file <path>" while their grant allows only the exact `--file <dispatch>/review-submit.txt` command. D33e9b58f piped
its review on stdin and was denied; D9a0af981 had other chained commands denied and never tried the submit."""
from __future__ import annotations

from pathlib import Path

from office import briefs
from office import review_submit

OUT = Path("/s/runs/R/dispatches/D1/reply.txt")


def test_no_brief_offers_a_stdin_submit_and_each_says_run_it_on_its_own():
    texts = [v for k, v in vars(briefs).items() if k.isupper() and isinstance(v, str) and "office review submit" in v]
    assert len(texts) >= 4
    for t in texts:
        assert "on stdin" not in t
        assert "that exact command on its own" in t and "does not mean it is denied" in t


def test_the_prompt_names_the_exact_command_alone_and_keeps_the_no_shell_fallback():
    p = review_submit.pointer(OUT, prompt_file="/s/brief.md")
    assert f"run exactly: {review_submit.submit_command(OUT)} , as one command on its own" in p
    assert "allowed even when other shell commands are denied" in p
    assert f"Only if you have no shell tool, or that exact command itself fails, write the complete review to {OUT}" in p
