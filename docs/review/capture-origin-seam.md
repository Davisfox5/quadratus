# Harness capture follows the dispatch profile (O-NEXT-13)

## TL;DR

- 7403511 fixed the page the lead is *told* at dispatch. The harness still
  *captures* with whatever capture profile the live config holds at capture
  time. Told 5000, captured 6000, and nothing records it.
- Fixing only the URL is not enough. Four drifts (preview argv, env,
  readiness path, timeouts) leave `capture_page` identical and still change
  what gets served and captured. The whole `CaptureProfile` has to be fixed
  at dispatch.
- Proposal: record the whole profile in the contract (`capture_profile`,
  canonical JSON, harness tasks only), capture with that, record the live
  reading beside it as a `capture_profile:` mismatch, and refuse to capture
  when the contract holds no profile. About 35 lines across three files.
- Profile removed mid-task: capture still runs with the contract's profile
  (what the lead was told), the removal is recorded, and the run cannot count
  as complete. Today it hands `None` to `capture_task`, which raises
  `AttributeError` in `capture_argv` and stops the task as an unknown
  exception. It never falls back to self-capture in either version.
- Old records: a harness contract written before the field existed is
  reported missing `capture_profile (absent: recorded before this field
  existed)`, the same treatment `design_instruction` got.

## Read map

Every read of `self.config.capture_profile`, direct or through a helper, at
7403511. "Post-dispatch" means after `_build_contract` at session.py:2014.

| Where | Reads | Decides | Post-dispatch? | Status |
|---|---|---|---|---|
| session.py:2785 | direct | whether the orchestrator prompt asks for a SCOPE capture block | no, orchestrator prompt | out of scope |
| session.py:3633 | direct | refuses a harness UI task inside a parallel batch | no, batch parse | out of scope |
| session.py:3132 -> 4259, 4270 `_capture_problem` | direct, `profile.origin` | refuses a UI task without a capture, checks the declared page and steps against a RESOLVES finding's `target` | no, runs before `run_task` | out of scope (a dispatch-time read) |
| session.py:2072 `_build_contract` -> 4220 `_harness_captures` | direct | `design_evidence` = "harness" | at dispatch | the fixing read |
| session.py:2081 -> 4228 `_live_design_instruction` | via `_harness_captures` | `design_instruction` | at dispatch | the fixing read |
| session.py:2117 -> 4232 `_live_capture_page` | direct, `profile.origin` | `capture_page` | at dispatch | the fixing read |
| session.py:4246-4247 `_design_instruction` | via `_live_capture_page` | lead prompt page (legacy side only, contract wins) | yes | fixed by 7403511, recorded |
| session.py:3309 `_brief_design_reviewers` | via `_harness_captures` | whether reviewers are shown the draft renders | yes | **O-NEXT-12 owns it, untouched here** |
| session.py:3339 `_check_design` | via `_harness_captures` | legacy side of `design_evidence` only; `_required` returns the contract | yes | already contract-bound, recorded |
| **session.py:4303 `_harness_capture`** | **direct** | **the profile handed to `preview.capture_task`: preview argv, origin (port, stray-listener check, ready URL, capture target), ready path and status, timeouts, env** | **yes** | **the seam; reached from 3352 (first capture) and 3391 (design-fix recapture)** |

Adjacent, not a profile read: after capture, `check_records`
(design_evidence.py) validates the summary but never compares its `target`
with `contract.capture_page`, and `_record_audit_findings` stores that
`target` as the finding's page. So a drifted capture also writes the wrong
page into a finding that `_capture_problem` later matches against. The
proposed change removes the cause; an explicit summary-target check is a
possible follow-up and is not in the diff.

## What is stubbed

Only `quadratus.preview.capture_task`, which `_harness_capture` imports at
call time (session.py:4300). The stub records the profile it was handed and
returns `""` or a given `CaptureFailure`. No preview starts and no browser
runs. The Session, its contract (`_build_contract` + `_bind_contract`), the
eligibility check, the dependency check and the source fingerprint are real.
Drift is a synthetic write to `session.config` between dispatch and capture.

## Tests (tests/lifecycle/test_capture_origin_applicability.py)

Strict xfails (`raises=AssertionError`), each failing at the assertion under
test with `--runxfail`:

1. origin drift 5000 -> 6000: capture_task is handed 6000.
2. profile removed: capture_task is handed `None`.
3. same-page drift, parametrized: preview argv, env, ready path, timeouts.
   The test first asserts live and contract `capture_page` are equal (the
   drift is invisible to the URL), then fails on the profile handed over.
4. design-fix recapture after drift: second capture goes to 6000.
5. no contract for this task (outcome still carries one, so eligibility
   passes): capture_task is called with the live profile instead of refusing.

Controls that pass today and must keep passing: no-drift capture, no-drift
recapture, a `CaptureFailure` passes through with its `origin`, and an
ineligible task stops before the boundary.

## Key question: URL or whole profile

Whole profile. Evidence:

- Test 3: four drifts keep `capture_page` byte-identical and still change
  the capture. `preview` changes what is served, `env` changes how it
  behaves, `ready_path` changes when the page counts as ready, and the
  timeouts change whether the capture completes.
- `preview.running` (preview.py:370-376) takes the port for its
  stray-listener refusal and the ready URL from `profile.origin`, while
  `capture_argv` (preview.py:424) builds the target from the same origin.
  Passing a pinned URL next to a live profile would start the preview on the
  live port and capture the pinned one. Whatever listens on the old port
  would then stand in for the preview, which is the exact case the
  stray-listener check exists to stop.
- `_task_gate` (session.py:2011) is the precedent: the gate the contract
  describes is held whole, not summarized.

`capture_page` stays as it is. It is what the lead reads, and it equals
`origin + scope.capture.path` of the recorded profile.

## Profile gone at capture time

Two different states:

- Live profile removed after dispatch, contract holds one: capture with the
  contract's profile, record `capture_profile: live differs from contract in
  profile (removed)`. Mismatches become `contract mismatch:` entries in
  `missing_facts` (outcome.py:259), so the run cannot count as complete.
  The design evidence stays "harness"; nothing falls back to self.
- No profile in the contract for this task (no contract, a contract for
  another task, or a harness contract without the field): refuse with
  `the harness did not capture because the task's contract holds no capture
  profile`. That flows into the existing `_check_design` failure path:
  `invalid_proof` open finding, `design_unverified`, no design-fix call on
  the first capture. The live profile is never consulted.

## Proposed diff (text only, not applied)

Checked in a scratch copy: with it, all 12 tests pass under `--runxfail`, and
the 8 strict xfails turn into strict XPASS failures (the signal to drop the
markers).

```diff
diff --git a/quadratus/contract.py b/quadratus/contract.py
@@ -77,6 +77,9 @@ class TaskContract:
     #: The full page URL (origin + path) a "harness" design instruction names,
     #: fixed at dispatch so the prompt never reads the live capture profile.
     capture_page: Optional[str] = None
+    #: The whole capture profile (canonical JSON) a "harness" capture runs
+    #: under, fixed at dispatch: the page alone does not fix what is served.
+    capture_profile: Optional[str] = None

@@ -88,7 +91,7 @@ class TaskContract:
     def to_dict(self) -> dict:
         data = asdict(self)
-        for name in ("scope", "intended_state", "inherits"):
+        for name in ("scope", "intended_state", "inherits", "capture_profile"):
             if data[name] is not None:
                 data[name] = json.loads(data[name])
diff --git a/quadratus/outcome.py b/quadratus/outcome.py
@@ -320,6 +320,11 @@ def _contract_missing(task: TaskOutcome) -> List[str]:
     elif required[_INSTRUCTION] == "harness" and not _nonempty(contract.get("capture_page")):
         missing.append(f"{tid}.contract.capture_page")
+    if required.get("design_evidence") == "harness":
+        if "capture_profile" not in contract:
+            missing.append(f"{tid}.contract.capture_profile (absent: recorded before this field existed)")
+        elif not isinstance(contract.get("capture_profile"), dict):
+            missing.append(f"{tid}.contract.capture_profile")
     return missing
diff --git a/quadratus/session.py b/quadratus/session.py
@@ -2114,7 +2114,9 @@ class Session:
             inherits=canonical(inherits),
-            capture_page=self._live_capture_page(spec) if required.design_instruction == "harness" else None)
+            capture_page=self._live_capture_page(spec) if required.design_instruction == "harness" else None,
+            capture_profile=(canonical(dataclasses.asdict(self.config.capture_profile))
+                             if evidence == "harness" else None))
@@ -4297,15 +4299,40 @@ class Session:
         ineligible = self._capture_ineligible()
         if ineligible:
             return f"the harness did not capture because {ineligible}"
+        profile = self._dispatch_capture_profile()
+        if profile is None:
+            return "the harness did not capture because the task's contract holds no capture profile"
         from .preview import capture_task
         self._verify_dependencies(f"before preview ({spec.task_id})")
         before = self._source_fingerprint()
-        failure = capture_task(self.config.capture_profile, self.project, spec.task_id, spec.scope.capture)
+        failure = capture_task(profile, self.project, spec.task_id, spec.scope.capture)
         self._verify_dependencies(f"during preview ({spec.task_id})")
@@
+    def _dispatch_capture_profile(self):
+        """The capture profile fixed in the current task's contract, the live
+        one recorded beside it as a mismatch. None when this task has no
+        contract or its contract holds none: the capture then refuses, and
+        never reads live config or falls back to a self-capture."""
+        from .preview import CaptureProfile
+        outcome, contract = self._outcome, getattr(self, "_contract", None)
+        if (outcome is None or contract is None or contract.task_id != outcome.task_id
+                or not contract.capture_profile):
+            return None
+        data = json.loads(contract.capture_profile)
+        held = CaptureProfile(**dict(data, preview=tuple(data["preview"]),
+                                     env=tuple(tuple(pair) for pair in data["env"])))
+        live = self.config.capture_profile
+        if live != held:
+            moved = ([f.name for f in dataclasses.fields(held) if getattr(held, f.name) != getattr(live, f.name)]
+                     if live is not None else ["profile (removed)"])
+            note = f"capture_profile: live differs from contract in {', '.join(moved)}"
+            if note not in outcome.mismatches:
+                outcome.mismatches.append(note)
+        return held
```

Why read the profile back out of the contract instead of holding the object
beside it like `_task_gate`: the first scratch version held
`self._task_capture_profile` at session.py:2011, and every direct-dispatch
test refused to capture because only `run_task` sets that attribute. Reading
the recorded JSON means the profile used is, by construction, the profile on
the record.

## Old records and other notes

- A pre-change harness contract has no `capture_profile` key and is reported
  as absent, never read as "no profile". Non-harness contracts are not
  asked for it.
- The contract digest now covers one more field (None for every non-harness
  task). Nothing re-verifies stored digests, so old records are unaffected.
- `env` values go into the run record. Profile validation already refuses
  credential-shaped names (`_ENV_RESERVED`, preview.py:54). If values should
  stay out of records anyway, store a sha256 of the canonical JSON in the
  contract and hold the object in memory; the mismatch would then name only
  that the profile changed, not which field.
- `_brief_design_reviewers` (session.py:3309) still reads
  `_harness_captures` live. O-NEXT-12 owns it; this diff does not touch it.
