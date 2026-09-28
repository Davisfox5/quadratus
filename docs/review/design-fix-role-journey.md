# J31 design-fix role packet journey

This is an offline, policy-backed whole-Session replay for workflow-map J31/G11.
It starts a frontend task, lets the lead edit and capture stale renders, lets a
revision edit the stylesheet, and reaches the one authorized `design-fix` call.
The fake provider writes current evidence in one case and leaves it stale in
the other. The controller, policy loader, Fleet dispatcher, change declaration
check, integration gate and closeout are real; only vendor replies are scripted.

## Behavior observed at `9eabf69`

The `design-fix` invocation receives `Role: lead` and the lead-only `Stop with
an ASK` checklist item. It does not receive `Role: reviewer`. The repair still
uses the original lead vendor and the same CLI arguments as the revision call;
the packet change does not widen the tool mode. The task's path scope remains
in the prompt, and the replay preserves the lead's HTML edit and revision's CSS
edit. Both the initial and post-fix integration checks pass.

When the repair captures current evidence, exactly one design review approves
it. The run then stops at the configured one-task cap, without claiming that
the overall goal was confirmed. When the repair leaves the evidence stale,
there is still exactly one `design-fix` call. It ends `DesignUnverified`, with
no design review or further repair call. This is the existing recovery bound.

On the immediate pre-G11 base `a582a26`, both parametrized cases fail at the
packet assertion: the emitted repair prompt has no `Role: lead` line. The
existing seam control had already identified the reviewer packet there. Thus
the whole-run test detects the intended route change rather than merely
repeating a passing prompt assertion.

## Assertion and limits

The target J31 assertion is that a design repair uses the lead's role contract
while keeping its task scope, writing mode and single recovery attempt. The
test asserts the emitted packet and the two terminal routes through a full
session; it does not assert that a live model will obey the instructions or
that browser renders depict the intended change. Scripted evidence is a
controller input. The local run does not exercise a vendor, a live browser, or
an operator capture profile.
