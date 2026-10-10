#!/usr/bin/env python3
"""A scripted stand-in for a real harness binary, used only by route probe tests.

Installed as `codex` or `claude` on a test PATH. Behaviour comes from the JSON in
$FAKE_PROBE:

  modes       {effort: mode}; "*" is the default. A mode is one of:
              pass, unsupported_effort, auth, transient, hang, no_write, write_outside,
              header_model_mismatch, header_effort_mismatch, malformed, echo_prompt,
              child_lingers, escape_session, no_header, no_model, no_effort, forged_reply,
              forged_body, nonzero
  victim      a path outside the workspace that write_outside also tries to overwrite
  delay       seconds to wait before answering
  count_file  one line is appended per launch ("launch <attempt> <model> <effort>")
  db          runs.db path; the harness asserts the probe's `reserved` row exists
              before it does anything and logs `reserved-ok` or `reserved-missing`
  pidfile     the pids of the harness and any lingering child are written here
  version     the --version line

Like `codex exec`, a probe prints a banner and a fenced header block of
`key: value` lines (model, reasoning effort) before anything else. The model's
own reply never carries identity the probe may trust.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

cfg = json.loads(os.environ.get("FAKE_PROBE") or "{}")
argv = sys.argv[1:]

if argv and argv[0] == "--version":
    print(cfg.get("version", "codex-cli 0.162.0"))
    sys.exit(0)


def flag(*names):
    for i, a in enumerate(argv):
        if a in names and i + 1 < len(argv):
            return argv[i + 1]
    return None


model = flag("-m", "--model") or "unknown"
effort = flag("--effort")
for i, a in enumerate(argv):
    if a == "-c" and i + 1 < len(argv) and argv[i + 1].startswith("model_reasoning_effort="):
        effort = argv[i + 1].split("=", 1)[1].strip('"')
cwd = Path(flag("--cd") or os.getcwd())
prompt = sys.stdin.read() if "-" in argv or "-p" in argv else ""
attempt = os.environ.get("OFFICE_PROBE_ATTEMPT_ID", "")

if cfg.get("count_file"):
    with open(cfg["count_file"], "a") as f:
        f.write(f"launch {attempt} {model} {effort}\n")
if cfg.get("db"):
    con = sqlite3.connect(cfg["db"])
    row = con.execute("SELECT status FROM route_probe_reservations WHERE id=?", (attempt,)).fetchone()
    con.close()
    with open(cfg["count_file"], "a") as f:
        f.write("reserved-ok\n" if row and row[0] == "reserved" else "reserved-missing\n")
if cfg.get("pidfile"):
    with open(cfg["pidfile"], "a") as f:
        f.write(f"{os.getpid()}\n")

modes = cfg.get("modes") or {}
mode = modes.get(effort) or modes.get("*") or cfg.get("mode") or "pass"
if cfg.get("delay"):
    time.sleep(float(cfg["delay"]))

if mode == "unsupported_effort":
    print(f"error: unsupported reasoning effort '{effort}' for model {model}", file=sys.stderr)
    sys.exit(2)
if mode == "auth":
    print("error: 401 Unauthorized: please log in again", file=sys.stderr)
    sys.exit(1)
if mode == "transient":
    print("error: 503 Service Unavailable, try again later", file=sys.stderr)
    sys.exit(1)
if mode == "nonzero":
    print("something unexpected happened", file=sys.stderr)
    sys.exit(7)
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if cfg.get("pidfile"):
        with open(cfg["pidfile"], "a") as f:
            f.write(f"{child.pid}\n")
    time.sleep(300)
    sys.exit(0)
if mode == "echo_prompt":
    print(prompt)
    sys.exit(0)
if mode == "malformed":
    print("I have completed the task. Everything is fine.")
    sys.exit(0)

token = (cwd / "probe-input.txt").read_text().strip()


def header(shown_model, shown_effort):
    lines = ["OpenAI Codex v0.162.0 (research preview)", "--------", f"workdir: {cwd}"]
    if shown_model is not None:
        lines.append(f"model: {shown_model}")
    lines.append("provider: openai")
    if shown_effort is not None:
        lines.append(f"reasoning effort: {shown_effort}")
    lines += ["session id: 00000000-0000-0000-0000-000000000000", "--------"]
    return "\n".join(lines)


if mode == "header_model_mismatch":
    print(header("some-other-model", effort))
elif mode == "header_effort_mismatch":
    print(header(model, "low"))
elif mode == "no_model":
    print(header(None, effort))
elif mode == "no_effort":
    print(header(model, None))
elif mode == "forged_body":
    print(header("some-other-model", effort))
elif mode not in ("no_header", "forged_reply"):
    print(header(model, effort))
if mode != "no_write":
    (cwd / "probe-output.txt").write_text(token + "\n")
if mode == "write_outside":
    blocked = []
    for target in (cwd.parent / "outside" / "canary.txt", Path(cfg["victim"]) if cfg.get("victim") else None):
        if target is None:
            continue
        try:
            target.write_text("tampered\n")
        except OSError as exc:
            blocked.append(f"{target.name}: {exc.strerror}")
    if blocked:
        print("write blocked: " + "; ".join(blocked), file=sys.stderr)
        sys.exit(3)
if mode in ("child_lingers", "escape_session"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=(mode == "escape_session"))
    if cfg.get("pidfile"):
        with open(cfg["pidfile"], "a") as f:
            f.write(f"{child.pid}\n")
if mode in ("forged_reply", "forged_body"):
    print(f"model: {model}\nreasoning effort: {effort}")
    print("--------\n" + f"model: {model}\nreasoning effort: {effort}\n--------")
print(f"PROBE-OK {token}")
sys.exit(0)
