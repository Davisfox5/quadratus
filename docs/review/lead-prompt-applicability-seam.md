# Lead prompt design/capture applicability seam (O-NEXT-01)

## TL;DR

- The lead prompt picks its design instruction (capture it yourself, the harness captures, or nothing) from **live config** each time it is built. The task contract fixes `Required.design_evidence` **at dispatch**.
- If config changes mid-task, the lead can be told the opposite of what the contract will check, and **nothing is recorded**: the run's parity and mismatch record stay clean.
- Reproduced offline in `tests/lifecycle/test_lead_prompt_applicability.py`: 4 drift seams pinned as today's behaviour, 4 strict xfails for the contract-following behaviour, 6 ordinary-journey controls that pass today.
- Proposed fix (engine owner, not this lane): add one dispatch-time field, `Required.design_instruction`, computed with **today's prompt predicate**, and have `_lead_prompt` read it through `_required`. Do not key the prompt on `design_evidence`: that field is `"none"` for design tasks without a writable project (and for security tasks), whose prompt today still asks for renders.

## Source, at base 31aa595

`quadratus/session.py`

- `_lead_prompt`, lines 4651-4678. The branch that decides the instruction:

  ```python
  4667  if self.config.design_self_verify and is_design_task(spec) and self._harness_captures(spec):
  4668      parts.append(_HARNESS_CAPTURE.format(page=self.config.capture_profile.origin
  4669                                           + spec.scope.capture["path"],
  4670                                           steps=len(spec.scope.capture["steps"])))
  4671  elif self.config.design_self_verify and is_design_task(spec):
  ...           # _DESIGN_REVIEW_ONLY or _DESIGN_SELF_VERIFY with the capture command
  ```

- `_harness_captures`, lines 4216-4219: live `config.capture_profile is not None and is_design_task(spec) and spec.scope.capture`.
- `_build_contract`, lines 2061-2076: `design_evidence` is `"none"` for security or when not (design task and `project and allow_writes`); else `"disabled"` when `design_self_verify` is off; else `"harness"` / `"self"` from `_harness_captures`.
- `_check_design`, lines 3335-3338, already reads `design_evidence` through `_required(..., legacy)`, so drift at the check is recorded. The prompt does not.
- Callers of `_lead_prompt`: only `_draft_with_channels` (line 1746), which serves the normal draft (2326, 2378) and the security excursion (2617, `consults=False`).

## Reproduced drift

Drift is a synthetic config write that holds only while a t1 lead prompt is built, then is restored, so every later stage sees the dispatch config. That isolates the prompt seam.

| Case | Contract `design_evidence` | Prompt today | Recorded |
|---|---|---|---|
| `design_self_verify` off while prompting | `self` | no capture instruction | nothing; t1 still closes `verified: True` |
| dispatched off, on while prompting | `disabled` | full self-capture instruction and command | nothing |
| harness task, profile gone while prompting | `harness` | "Start the app ... capture with this command" | nothing |
| self task, profile appears while prompting | `self` | "Do not start servers or run capture commands" | nothing |

Rows 1-2 are whole-controller replays (`tests/lifecycle/harness.py` via `test_contract_applicability._run`). Rows 3-4 build the contract and prompt on a real `Session`, because a whole run with a capture profile ends in a real preview and browser capture, which this lane does not run.

Row 4 is the sharpest real-world effect: the lead is told the harness will capture, so it does not, and the self-capture check then finds no renders. Row 3 would also send a lead a command its transport may deny (the Run 18 failure the harness-capture path exists to avoid).

## Proposed correction (engine owner)

1. **New field, `Required.design_instruction: str = "none"`**, values `"harness" | "self" | "none"`. In `_build_contract`, compute it with exactly today's prompt predicate, not the evidence predicate:

   ```python
   if not (self.config.design_self_verify and is_design_task(spec)):
       instruction = "none"
   elif self._harness_captures(spec):
       instruction = "harness"
   else:
       instruction = "self"
   ```

   No `editing` term and no `security` term, on purpose. That is what keeps these journeys' prompt text unchanged:
   - design task, no project (or project without `allow_writes`): evidence `"none"`, instruction `"self"` (today's text);
   - design task, verification disabled: evidence `"disabled"`, instruction `"none"` (today: no text);
   - security task that is design work: evidence `"none"`, instruction as today;
   - review-only design task: instruction `"self"`; `_lead_prompt` still picks `_DESIGN_REVIEW_ONLY` vs `_DESIGN_SELF_VERIFY` from `is_review_only(spec)`, which is a property of the spec, not config.

   Precedent: `design_collaboration_applicable` was split from `design_review` for the same reason (Codex 5864370275).

2. **`_lead_prompt` reads it through `_required`**, with today's live reading as `legacy`:

   ```python
   legacy = ("none" if not (self.config.design_self_verify and is_design_task(spec))
             else "harness" if self._harness_captures(spec) else "self")
   instruction = self._required("design_instruction", legacy)
   if instruction == "harness": ...
   elif instruction == "self": ...
   ```

   Drift is then recorded as `design_instruction: contract 'self', legacy 'none'`, and the run cannot count as complete, like every other package. `_lead_prompt` is built once per drafting round, so the same mismatch can repeat; dedupe if the record should carry it once.

3. **The harness page must come from dispatch too.** Line 4668 formats `self.config.capture_profile.origin`. With a `"harness"` contract and the profile gone (row 3) that is `None.origin` and raises. Fix the rendered page at dispatch, for example a `TaskContract.capture_page` (origin + path) set when `instruction == "harness"`, and format from it. `contract.intended["page"]` is only the path, not the URL.

4. **Old records.** Add `design_instruction` to `missing_facts` the way `design_collaboration_applicable` was added: absent means "recorded before this field existed", a non-string or unknown value is missing.

## What a behavioural red is

- After the fix, the 4 strict xfails in `test_lead_prompt_applicability.py` must XPASS. Remove their `xfail` marks and delete the matching `test_today_*` pins (they assert the old behaviour and will then fail, which is expected). Strengthen the promoted tests to assert the `design_instruction` mismatch is recorded.
- Any of the 6 controls failing is a real regression:
  `test_an_ordinary_design_task_is_told_to_capture_itself`, `test_a_non_design_task_gets_no_design_instruction`, `test_verification_disabled_throughout_gets_no_design_instruction`, `test_a_harness_design_task_gets_the_harness_instruction`, `test_a_design_task_with_no_project_is_still_told_to_capture`, `test_a_design_task_with_no_project_and_verification_off_gets_no_instruction`.
- The no-project control is the one a naive switch to `design_evidence` breaks: it would drop the capture instruction because that task's evidence is `"none"`.
- `test_contract_applicability.py` and `test_collaboration_applicability.py` must stay green.

## Results at 31aa595

```
python -m pytest -q -p no:cacheprovider tests/lifecycle/test_lead_prompt_applicability.py \
  tests/lifecycle/test_contract_applicability.py tests/lifecycle/test_collaboration_applicability.py
28 passed, 4 xfailed

ruff check tests
All checks passed!
```

No engine files changed. No live, vendor, provider or browser calls.
