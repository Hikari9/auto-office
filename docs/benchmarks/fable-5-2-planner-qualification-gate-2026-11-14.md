# Fable 5.2 planner-only qualification gate (PR #1133, November 14, 2026)

All five catalog routes remain `dispatchable:false` and none is locally qualified. `route_policy.FABLE_PLANNER_QUALIFIED_EFFORTS` is intentionally empty. The hard role boundary rejects Fable for executor, worker, code/plan/integration/visual review and other non-planner roles, including direct overrides, alias identity, preferred seeds and generic user role allows. It also prevents unqualified planner dispatch despite a user allow. No existing default selection order changes. Qualification requires separately reviewed per-effort evidence and activation; do not populate the set from vendor documentation or AA headline scores.

The `office doctor --probe-route claude/claude-fable-5-2@<effort>` manual probe remains available, subject to live explicit user denials. It is **evidence gathering**, not planner activation. Use an isolated scratch repository and a low-risk synthetic planning prompt. For each of low, medium, high, xhigh, max:

1. Record `claude --version`, installed adapter fingerprint, exact Office invocation, selected-model *and selected-effort* readback, session transcript, exit code, and quota/account context. Include a deliberately different account default to catch silent model/effort fallback.
2. Verify planner tool profile, read-only planning behavior, sandbox and write boundaries, auth/permissions, fallback/unsupported-effort behavior, quota reserve, and model-specific retention rules. Check the actual CLI rather than assuming the API supports the same flags.
3. Validate that every non-planner role, explicit `--as`/`--review-as`, generic user role allow, alias and signed policy override is denied. Check pre-existing pinned runs, current role defaults, and packaged parity.
4. Run targeted tests, full repository CI and independent review on the final SHA. Only after receipts pass should a dedicated reviewed activation change grant a specific planner effort; no automatic default promotion.

AA v4.3.2 shipped scores are intentionally null. AA v5.0.1 published Intelligence Index values (54/56/62/62/65) remain versioned, non-routing research evidence. Hydrogen #1142 and the executor policy #1143 must remain separate.
