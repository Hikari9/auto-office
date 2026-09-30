#!/usr/bin/env python3
"""Scripted stand-in for codex/claude/gemini in Office tests.

Reads the prompt (stdin or last argv), finds the role from its first line and
performs the next scripted action for that role from $FAKE_SCENARIO (JSON):

  {"executor": [{"write": {"calc.py": "..."}, "submit": true}],
   "code_reviewer": [{"reply": "VERDICT: PASS"}],
   "plan_reviewer": [...], "planner": [{"plan": "...", "submit": true}],
   "visual_reviewer": [...], "probe": [{"reply": "auto"}]}

Actions may also set "exit", "signal", "sleep", "stderr", "ack", "raw".
The last action repeats when the list is exhausted.
"""
import json, os, re, signal, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
if "--version" in argv or argv[:1] == ["-v"]:
    print(f"{Path(sys.argv[0]).name} 1.0.0")
    sys.exit(0)
prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
if not prompt.strip() and argv:
    prompt = argv[-1]
    if prompt.startswith("--prompt="):
        prompt = prompt[len("--prompt="):]
first = prompt.strip().splitlines()[0] if prompt.strip() else ""
role = "unknown"
if first.startswith("ROLE executor"):
    role = "executor"
elif first.startswith("ROLE planner"):
    role = "planner"
elif "plan reviewer" in first:
    role = "plan_reviewer"
elif "integration reviewer" in first:
    role = "integration_reviewer"
elif "code reviewer" in first:
    role = "code_reviewer"
elif "conformance probe" in first:
    role = "probe"
elif "visual reviewer" in first:
    role = "visual_reviewer"
scenario = json.loads(Path(os.environ["FAKE_SCENARIO"]).read_text())
harness = os.environ.get("FAKE_HARNESS") or Path(sys.argv[0]).name
key = f"{harness}:{role}" if f"{harness}:{role}" in scenario else role
steps = scenario.get(key) or [{}]
counter = Path(os.environ["FAKE_SCENARIO"]).with_suffix(f".{key.replace(':', '_')}.count")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
action = steps[min(n, len(steps) - 1)]
log = Path(os.environ["FAKE_SCENARIO"]).with_suffix(".log")
with log.open("a") as fh:
    fh.write(json.dumps({"harness": harness, "role": role, "n": n, "argv": argv[:6], "cwd": os.getcwd(),
                         "env_run": os.environ.get("OFFICE_RUN_ID"), "env_dispatch": os.environ.get("OFFICE_DISPATCH_ID")}) + "\n")
if action.get("sleep"):
    time.sleep(action["sleep"])
if action.get("signal"):
    os.kill(os.getpid(), getattr(signal, "SIG" + action["signal"]))


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

def office(*args):
    return subprocess.run([sys.executable, "-m", "office", *args], capture_output=True, text=True)

out_file = None
if "-o" in argv and harness == "codex":  # codex: -o <file>; gemini: -o <format>
    out_file = argv[argv.index("-o") + 1]
reply = action.get("reply")
if reply == "auto" and role == "probe":
    # A route that can see: actually decode the probe PNG's pixels.
    images = [m.lstrip("@") for m in re.findall(r"(@?\S+probe\.png)", prompt + " " + " ".join(argv))]
    reply = "PROBE NO_IMAGE"
    if images and Path(images[0]).is_file():
        reply = "PROBE " + read_probe(Path(images[0])) + "\nEVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS"
writes = dict(action.get("write") or {})
writes.update((action.get("write_by_task") or {}).get(os.environ.get("OFFICE_TASK_ID", ""), {}))
for path, content in writes.items():
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)
if action.get("git_commit"):
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=w", "commit", "-qm", action["git_commit"]], check=True)
if action.get("plan") is not None:
    draft = Path(".office/plans") / os.environ["OFFICE_RUN_ID"].split("-", 1)[0][:8] / "PLAN.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(action["plan"])
if action.get("ack"):
    r = office("ack", action["ack"])
    print(r.stdout)
if action.get("submit"):
    r = office("submit")
    print(r.stdout + r.stderr)
    if action.get("ack_after_submit"):
        m = re.search(r"AMENDMENT (A\d+)", r.stdout)
        if m:
            print(office("ack", m.group(1)).stdout)
            print(office("submit").stdout)
if action.get("raw"):
    print(action["raw"])
if reply is not None:
    if out_file:
        Path(out_file).write_text(reply)
    else:
        print(reply)
if action.get("stderr"):
    sys.stderr.write(action["stderr"])
sys.exit(action.get("exit", 0))
