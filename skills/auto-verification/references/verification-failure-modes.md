# Verification failure modes

## green-suite-double

When every test substitutes a test double for the same dependency, no test exercises the real
construction path. The suite reports the doubles are consistent with each other, which is not the
claim anyone wanted.

The failure shape: a shared factory gains a required argument; every call site in the suite
passes a double instead, so the suite stays green while every real invocation raises immediately
on first contact with the live system. Test count and pass rate are unchanged, so nothing in the
report signals the gap.

See auto-verification/SKILL.md, "A green suite that injects a double at every seam proves the
double," for the remedy this failure mode motivates.

## free-form-capture

A low-cost model drives a browser from a prose brief and picks its own clicks and timing. Across
several rounds of one run, it captured frames before rendering settled, followed a different click
path than the step named, and once left the safe simulation mode that the brief listed as a hard
rule, so the frames had to be deleted and recaptured. Separately, the browser tool could not resize
its viewport, so the phone-width steps came back as desktop frames. Every round still reported
success. After the orchestrator switched to a pinned steps file run by a fixed script, with a DOM
probe per state, none of these recurred. The worker's job shrank to starting the server, running the
script and stopping the server, and acceptance was settled on the probe values.

See auto-verification/SKILL.md, "A cheap browser worker captures; it does not verify," for the
remedy.
