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
