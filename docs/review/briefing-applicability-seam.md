# Briefing applicability seam (O-NEXT-12)

## TL;DR

- `Session._brief_design_reviewers` still asks the live capture profile
  (`_harness_captures(spec)`) who renders a design task. The lead prompt
  stopped doing that in 7403511 and reads the contract instead.
- If the profile changes after dispatch, the reviewers' briefing and the
  lead's instruction disagree, and nothing is recorded. Both directions
  reproduce in `tests/lifecycle/test_briefing_applicability.py` (two strict
  xfails, six passing controls).
- Proposed fix: one line. Read `Required.design_instruction` through
  `_required`, with `_live_design_instruction(spec)` as the legacy value, and
  skip the briefing when it is `"harness"`. No new contract field, so no
  old-record handling.
- Not an engine edit yet. The diff below is text only. The harness-capture
  seam (`_harness_capture`) is O-NEXT-13's and is untouched.

## What goes wrong

The briefing runs after the draft (`run_task`, right after `_assess_scope`).
Today:

```python
if not (self._collaboration_applicable(spec) and self.project):
    return
if self._harness_captures(spec):   # live config
    return
... check(...)                     # the lead's own renders
```

| Contract at dispatch | Live profile at briefing | Lead was told | Briefing today | Recorded |
|---|---|---|---|---|
| `self` | appears | capture yourself | returns early, reviewers never see the renders | nothing |
| `harness` | vanishes | the harness captures | reads the draft's evidence folder and briefs from it | nothing |

The second row is the worse one: the reviewers are handed a draft render the
lead was told not to make, whatever happens to be in that folder.

## Which contract field

`design_instruction`, not `design_evidence`.

The briefing's question is "did the lead render this draft itself". That is
exactly what the lead prompt was told, and `design_instruction` is the
field the prompt reads (`Session._design_instruction`). Keying the briefing
on it makes the prompt and the briefing agree by construction.

`design_evidence` answers a different question, "who proves the final
state", and differs from the instruction where the briefing can still run:

- **Project set, `allow_writes` off.** Evidence is `"none"`; the instruction
  is `"self"` or `"harness"`. The briefing passes its `self.project` check
  and runs today. Keying on `evidence == "harness"` would read the draft's
  folder for a task whose lead was told the harness captures; keying on
  `evidence == "self"` would stop briefing tasks that are briefed today.
- **Security.** Evidence is `"none"`, the instruction is not. Security tasks
  leave `run_task` for `_run_security_task` (session.py:2281) before the
  briefing, so this does not reach it today, but it is the same split the
  lead-prompt controls pin.

## The change

```python
if self._required("design_instruction", self._live_design_instruction(spec)) == "harness":
    return
```

- `_required("design_instruction", legacy)` returns the contract's value and
  records `design_instruction: contract X, legacy Y` when the live reading
  differs, the same string the lead prompt records. With no contract for the
  task, legacy decides and a `contract missing` mismatch is recorded.
- Legacy value: `self._live_design_instruction(spec)`. It has the same
  domain as the field (`"harness" | "self" | "none"`), and it is what the
  field was computed from at dispatch, so a no-drift run never records a
  mismatch.
- It stays after the `self.project` check, so a no-project design task
  (contract `none` / instruction `self`) reads nothing and records nothing.
- Old records: none needed. `design_instruction` already exists and
  `missing_facts` already reports it as absent on records from before
  7403511. The capture page is not read here, so `capture_page` is not
  compared.

### One no-drift difference, stated

Verification disabled with a capture profile: the instruction is `"none"`,
so the fixed briefing calls `check` where today it returns early. The lead
was not asked to capture, so the folder is normally empty and nothing is
briefed; the control
`test_verification_disabled_with_a_profile_is_not_briefed` pins that with
the real `check`. If a lead left renders unasked, reviewers would now see
them, the same as disabled without a profile today. The early return in
that case rested on "harness renders come later", which is false when
verification is off: the enforced check records `disabled` and captures
nothing.

## Proposed diff (text only, not applied)

```diff
diff --git a/quadratus/session.py b/quadratus/session.py
--- a/quadratus/session.py
+++ b/quadratus/session.py
@@ -3306,9 +3306,11 @@ class Session:
         self._review_evidence_hashes, self._review_snapshot = {}, None
         if not (self._collaboration_applicable(spec) and self.project):
             return
-        if self._harness_captures(spec):
-            # Harness renders are taken after the final edit and a passing
-            # gate, so collaborators are promised none of the draft.
+        # Who renders is what the lead was told at dispatch (map P3.4), the
+        # live reading recorded beside it. Harness renders are taken after
+        # the final edit and a passing gate, so collaborators are promised
+        # none of the draft.
+        if self._required("design_instruction", self._live_design_instruction(spec)) == "harness":
             return
         from .design_evidence import check
         ok, _, shots = check(self.project, spec.task_id, self._last_edit_started or 0,
```

Applied in a scratch run only: all eight tests in the new file pass under
`--runxfail`. The xfail markers come off with the engine change.

## Tests

`tests/lifecycle/test_briefing_applicability.py`, built on a real `Session`
with `_session` / `_dispatch` as in `test_lead_prompt_applicability.py`. The
drift is a config write that holds only while `_brief_design_reviewers`
runs.

Stubbed: `quadratus.design_evidence.check`, only where the lead's renders
must pass (a real pass needs a browser capture). It returns
`(True, "", [desktop, mobile])` and counts calls. The evidence files are
written to disk, so `_evidence_files` and `runtime.evidence_refusals` run
for real. The disabled controls use the real `check`; the no-project
control stubs it only to prove it is not called.

- xfail: self task, profile drifts in: briefed, `check` called once,
  mismatch `design_instruction: contract 'self', legacy 'harness'`.
- xfail: harness task, profile drifts away: not briefed, `check` not
  called, mismatch `design_instruction: contract 'harness', legacy 'self'`.
- controls (no drift, no mismatches): self briefed; harness not briefed;
  disabled with and without a profile not briefed; non-design not briefed;
  no-project not briefed.
