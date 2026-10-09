#!/usr/bin/env python3
"""Scripted stand-in for codex/claude/gemini in Office tests.

Reads the prompt (stdin or last argv), finds the role from its first line and
performs the next scripted action for that role from $FAKE_SCENARIO (JSON):

  {"executor": [{"write": {"calc.py": "..."}, "submit": true}],
   "code_reviewer": [{"reply": "VERDICT: PASS"}],            (v3.1 per-task review)
   "convergence_reviewer": [{"reply": "VERDICT: APPROVED\nNEXT proceed"}],   (#337 lane review)
   "plan_reviewer": [...], "planner": [{"plan": "...", "submit": true}],
   "visual_reviewer": [...], "probe": [{"reply": "auto"}]}

Actions may also set "exit", "signal", "sleep", "stderr", "ack", "raw".
The last action repeats when the list is exhausted.

Two ways to run it. As a script (the harness binaries wrap it) it is a real
process. `run(...)` runs the same logic inside the calling process, which the
test launcher uses so a scenario costs no interpreter start; `office` commands
the agent issues then go through `office.cli.main` instead of a child process.
"""
import contextlib, io, json, os, re, signal, subprocess, sys, time, traceback
from pathlib import Path
from types import SimpleNamespace

# A signal whose default action ends a Python process silently, so `-signum`
# is exactly what a real child's exit status would have been.
EMULATED_SIGNALS = ("TERM", "KILL", "HUP")


def scenario_runs_in_process(env):
    """Whether every action of the scenario can be reproduced by `run`: a
    `sleep` (wall caps, stops) and any other `signal` need a real process."""
    try:
        scenario = json.loads(Path(env["FAKE_SCENARIO"]).read_text())
        actions = [a for steps in scenario.values() if isinstance(steps, list) for a in steps]
        return not any(a.get("sleep") or (a.get("signal") and a["signal"] not in EMULATED_SIGNALS) for a in actions)
    except (KeyError, OSError, ValueError, AttributeError):
        return False


def read_probe(path):
    import struct, zlib
    data = path.read_bytes()
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    px = lambda x, y: tuple(raw[y * stride + 1 + x * 3: y * stride + 4 + x * 3])
    font = {"0": ["01110","10001","10011","10101","11001","10001","01110"], "1": ["00100","01100","00100","00100","00100","00100","01110"],
            "2": ["01110","10001","00001","00010","00100","01000","11111"], "3": ["11110","00001","00001","01110","00001","00001","11110"],
            "4": ["00010","00110","01010","10010","11111","00010","00010"], "5": ["11111","10000","11110","00001","00001","10001","01110"],
            "6": ["00110","01000","10000","11110","10001","10001","01110"], "7": ["11111","00001","00010","00100","01000","01000","01000"],
            "8": ["01110","10001","10001","01110","10001","10001","01110"], "9": ["01110","10001","10001","01111","00001","00010","01100"]}
    scale, pad, digits = 12, 24, ""
    for i in range(4):
        rows = []
        for gy in range(7):
            row = ""
            for col in range(5):
                x, y = pad + (i * 6 + col) * scale + scale // 2, pad + gy * scale + scale // 2
                row += "1" if px(x, y) == (0, 0, 0) else "0"
            rows.append(row)
        digits += next((d for d, f in font.items() if f == rows), "?")
    r, g, b = px(w - pad - 80, pad + 10)
    color = "red" if r > 150 and g < 100 else ("green" if g > 120 and r < 100 else "blue")
    return f"{digits} {color}"


def _role(first):
    if first.startswith("ROLE executor"):
        return "executor"
    if first.startswith("ROLE planner"):
        return "planner"
    for marker, role in (("plan reviewer", "plan_reviewer"), ("integration reviewer", "integration_reviewer"),
                         ("convergence reviewer", "convergence_reviewer"),
                         ("code reviewer", "code_reviewer"), ("conformance probe", "probe"),
                         ("visual reviewer", "visual_reviewer")):
        if marker in first:
            return role
    return "unknown"


def _office_runner(env, cwd, in_process):
    """`office(*args)` -> an object with stdout, stderr and returncode."""
    def subprocess_office(*args):
        return subprocess.run([sys.executable, "-m", "office", *args], capture_output=True, text=True,
                              cwd=str(cwd), env=env)

    def inline_office(*args):
        from unittest import mock
        from office import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(sys, "argv", ["office", *args]))
            stack.enter_context(contextlib.chdir(cwd))
            try:
                code = cli.main(list(args))
            except SystemExit as exit_:
                code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 1)
            except Exception:
                err.write(traceback.format_exc())
                code = 1
        return SimpleNamespace(stdout=out.getvalue(), stderr=err.getvalue(), returncode=code)

    # A pinned-release hop replaces the process (execve): never in this one.
    return inline_office if in_process and "OFFICE_VERSION_OVERRIDE" not in env else subprocess_office


def _comply_with_self_review(cwd, env, writes, err):
    """What a compliant executor does before `office submit` (#421): commit the work it wrote, then write
    a clean ledger naming that HEAD. An action with "no_ledger": true skips both."""
    def git(*a):
        proc = subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=w", *a], cwd=str(cwd), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        err.write(proc.stdout)
        return proc.stdout.strip()
    git("add", "--", *writes)
    git("commit", "-qm", "work", "--allow-empty")
    head = git("rev-parse", "HEAD")
    lenses = "\n".join(f"LENS {n} reviewed" for n in ("security", "edge-cases", "platform", "test-strength"))
    (Path(cwd) / "OFFICE_SELF_REVIEW.md").write_text(f"COMMIT {head}\nROUND 1\n{lenses}\n")


def _act(argv, prompt, env, cwd, in_process, out, err):
    """Perform the next scripted action. Returns the exit code; a negative code
    is a death by that signal. Text for stdout and stderr goes to `out`, `err`."""
    args = argv[1:]
    if "--version" in args or args[:1] == ["-v"]:
        out.write(f"{Path(argv[0]).name} 1.0.0\n")
        return 0
    if not prompt.strip() and args:
        prompt = args[-1]
        if prompt.startswith("--prompt="):
            prompt = prompt[len("--prompt="):]
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    role = _role(first)
    scenario_file = Path(env["FAKE_SCENARIO"])
    scenario = json.loads(scenario_file.read_text())
    harness = env.get("FAKE_HARNESS") or Path(argv[0]).name
    key = f"{harness}:{role}" if f"{harness}:{role}" in scenario else role
    steps = scenario.get(key) or [{}]
    counter = scenario_file.with_suffix(f".{key.replace(':', '_')}.count")
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    action = steps[min(n, len(steps) - 1)]
    with scenario_file.with_suffix(".log").open("a") as fh:
        fh.write(json.dumps({"harness": harness, "role": role, "n": n, "argv": args[:6], "cwd": os.path.realpath(cwd),
                             "env_run": env.get("OFFICE_RUN_ID"), "env_dispatch": env.get("OFFICE_DISPATCH_ID")}) + "\n")
    if action.get("sleep"):
        time.sleep(action["sleep"])
    if action.get("signal"):
        return -int(getattr(signal, "SIG" + action["signal"]))
    office = _office_runner(env, cwd, in_process)

    def at(path):
        return Path(cwd) / path

    out_file = None
    if "-o" in args and harness == "codex":  # codex: -o <file>; gemini: -o <format>
        out_file = args[args.index("-o") + 1]
    reply = action.get("reply")
    if reply == "auto" and role == "probe":
        # A route that can see: actually decode the probe PNG's pixels.
        images = [m.lstrip("@") for m in re.findall(r"(@?\S+probe\.png)", prompt + " " + " ".join(args))]
        reply = "PROBE NO_IMAGE"
        if images and at(images[0]).is_file():
            reply = "PROBE " + read_probe(at(images[0])) + "\nEVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS"
    writes = dict(action.get("write") or {})
    writes.update((action.get("write_by_task") or {}).get(env.get("OFFICE_TASK_ID", ""), {}))
    for path, content in writes.items():
        at(path).parent.mkdir(parents=True, exist_ok=True)
        at(path).write_text(content)
    if action.get("git_commit"):
        # A child's own output is emitted at once, ahead of this script's buffered stdout.
        for cmd in (["git", "add", "-A"],
                    ["git", "-c", "user.email=a@b", "-c", "user.name=w", "commit", "-qm", action["git_commit"]]):
            proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            err.write(proc.stdout)
            proc.check_returncode()
    if action.get("plan") is not None:
        draft = at(".office/plans") / env["OFFICE_RUN_ID"].split("-", 1)[0][:8] / "PLAN.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(action["plan"])
    if action.get("ack"):
        r = office("ack", action["ack"])
        out.write(r.stdout + "\n")
    if action.get("submit") and role == "executor" and writes and not action.get("no_ledger"):
        _comply_with_self_review(cwd, env, writes, err)
    if action.get("submit"):
        r = office("submit")
        out.write(r.stdout + r.stderr + "\n")
        if action.get("ack_after_submit"):
            m = re.search(r"AMENDMENT (A\d+)", r.stdout)
            if m:
                out.write(office("ack", m.group(1)).stdout + "\n")
                out.write(office("submit").stdout + "\n")
    if action.get("raw"):
        out.write(action["raw"] + "\n")
    if reply is not None:
        if out_file:
            at(out_file).write_text(reply)
        else:
            out.write(reply + "\n")
    if action.get("stderr"):
        err.write(action["stderr"])
    return action.get("exit", 0)


def _execute(argv, prompt, env, cwd, in_process):
    out, err = io.StringIO(), io.StringIO()
    try:
        code = _act(argv, prompt, env, cwd, in_process, out, err)
    except Exception:
        err.write(traceback.format_exc())
        code = 1
    return code, out.getvalue(), err.getvalue()


def run(argv, prompt, env, cwd):
    """Run the agent for `argv` in this process: (exit code, merged output).

    The code is negative for a scripted death by signal, as a child's would be.
    A real child's stdout is block-buffered behind its unbuffered stderr, so the
    merged output lists stderr first.
    """
    code, out, err = _execute(argv, prompt, env, cwd, in_process=True)
    return code, (err + out).encode()


if __name__ == "__main__":
    version_probe = "--version" in sys.argv[1:] or sys.argv[1:2] == ["-v"]  # never waits on stdin
    code, out, err = _execute(sys.argv, "" if version_probe or sys.stdin.isatty() else sys.stdin.read(), dict(os.environ),
                              os.getcwd(), in_process=False)
    sys.stderr.write(err)  # stderr is unbuffered, stdout is flushed at exit
    sys.stderr.flush()
    sys.stdout.write(out)
    sys.stdout.flush()
    if code < 0:
        os.kill(os.getpid(), -code)
    sys.exit(code)
