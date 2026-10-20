"""Attempt 2 of the blind acceptance, turned into regressions.

The orchestrator thought aloud inside the task description, revised the
function signature mid-text and emitted SCOPE from its first draft; the lead
implemented one form and was measured against the other. The 100-line
estimate covered a validator plus ten named test scenarios and landed at 223,
all inside the slice, and the stop message asserted a cause the evidence did
not show. Three deterministic repairs: a signature lint that fails into the
existing correction round, a code/test split in the scope report, and a stop
message that reports the measurement without diagnosing it.
"""
import json

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.scope import (
    ScopeReport,
    TaskScope,
    count_change_lines_by_path,
    declared_signatures,
    is_test_path,
    read_scope,
)
from quadratus.session import RunStalled, Session, SessionConfig

SCOPE = dict(permitted_paths=["app.py", "tests/test_import_preview.py"],
             intended_result="A row validator `_validate_clip_row(project, cols, row)` in app.py",
             acceptance=["app.py defines `_validate_clip_row(project, cols, row)`",
                         "`python -m pytest -q` passes"],
             max_lines=100)

# The shape of the attempt-2 description, abridged: a def in a code block,
# then a mid-text revision of the same function.
CONTRADICTORY = (
    "Add the per-row validator.\n\n```python\ndef _validate_clip_row(project, cols, row):\n"
    "    \"\"\"Validate one CSV data row.\"\"\"\n```\n\n"
    "Rules:\n- Its width is passed as `len(cols)`? No, headers may include ignored columns; "
    "instead give the function signature `_validate_clip_row(project, cols, width, row)` "
    "and reject `len(row) != width`.\n- `Start (s)`: `float()` parse, `math.isfinite`.\n"
)
FINAL = (
    "Add the per-row validator.\n\n```python\ndef _validate_clip_row(project, cols, row):\n"
    "    \"\"\"Validate one CSV data row.\"\"\"\n```\n\n"
    "Rules:\n- reject `len(row) != len(cols)`.\n- `Start (s)`: `float()` parse, "
    "`math.isfinite`; `str.strip()` every cell.\n"
)


def _declaration(body, **overrides):
    return "KIND: backend simple\nSCOPE: " + json.dumps(dict(SCOPE, **overrides)) + "\n" + body


def test_the_attempt_two_shape_is_rejected_naming_both_signatures():
    with pytest.raises(ValueError) as info:
        read_scope(_declaration(CONTRADICTORY), max_lines=100)
    message = str(info.value)
    assert "_validate_clip_row(project, cols, row)" in message
    assert "_validate_clip_row(project, cols, width, row)" in message
    assert "final text" in message


def test_acceptance_that_disagrees_with_the_description_is_rejected():
    acceptance = ["app.py defines `_validate_clip_row(project, cols, width, row)`"]
    with pytest.raises(ValueError, match="more than one signature"):
        read_scope(_declaration(FINAL, acceptance=acceptance), max_lines=100)


def test_a_final_description_with_one_signature_passes():
    scope, body = read_scope(_declaration(FINAL), max_lines=100)
    assert scope.max_lines == 100 and "SCOPE:" not in body
    assert declared_signatures(body, [scope.intended_result], scope.acceptance) == {
        "_validate_clip_row": ["project, cols, row"],
    }


def test_incidental_calls_are_not_declared_functions():
    body = "Use `float(x)` here and `float()` there; `len(row)` and `len(cols)` differ."
    assert declared_signatures(body, ["Do it"], ["`python -m pytest -q` passes"]) == {}
    scope, _ = read_scope(_declaration(body), max_lines=100)
    assert scope.intended_result.startswith("A row validator")


def test_whitespace_and_trailing_colon_do_not_count_as_different_signatures():
    body = "```python\ndef f(a, b):\n```\nThen `f( a,b )` and `def f(a,  b):` again."
    assert declared_signatures(body, [], []) == {"f": ["a, b"]}


def test_a_lint_failure_feeds_the_existing_correction_round(tmp_path):
    calls = []
    replies = iter([_declaration(CONTRADICTORY), _declaration(FINAL)])

    def invoke(key, prompt, **kw):
        calls.append(prompt)
        return next(replies)

    session = Session("fix", ArtifactStore(tmp_path / ".quadratus"), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    spec = session.next_task()
    assert spec.scope.max_lines == 100 and "width" not in spec.description
    assert len(calls) == 2
    assert "CORRECTION REQUIRED: the declaration gives _validate_clip_row" in calls[1]
    assert "final text" in calls[1]


def test_a_second_contradiction_stalls_instead_of_dispatching(tmp_path):
    replies = iter([_declaration(CONTRADICTORY)] * 2)
    session = Session("fix", ArtifactStore(tmp_path / ".quadratus"), lambda *a, **k: next(replies),
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    with pytest.raises(RunStalled, match="more than one signature"):
        session.next_task()


DIFF = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
+def _validate_clip_row(project, cols, row):
+    return None, []
-old = 1
--- a/tests/test_import_preview.py
+++ b/tests/test_import_preview.py
@@ -0,0 +1,3 @@
+def test_a():
+    pass
+
"""


def test_assess_splits_changed_lines_between_code_and_tests():
    report = TaskScope(["app.py", "tests/"], "r", ["a"], 2).assess(DIFF)
    assert count_change_lines_by_path(DIFF) == {"app.py": 3, "tests/test_import_preview.py": 3}
    assert (report.changed_lines, report.code_lines, report.test_lines) == (6, 3, 3)
    assert report.oversized and report.within_scope


def test_the_oversized_message_reports_the_measurement_without_a_cause():
    report = ScopeReport(within_scope=True, changed=["app.py", "tests/test_import_preview.py"],
                         changed_lines=223, max_lines=100, code_lines=85, test_lines=138)
    text = report.render()
    assert "223 changed lines (85 in code, 138 in tests)" in text
    assert "~100" in text and "150" in text and "Tests count in full" in text
    assert "expanding" not in text and "whole feature" not in text
    assert "passed" not in text.lower()


@pytest.mark.parametrize("path, expected", [
    ("app.py", False), ("tests/test_x.py", True), ("test/util.py", True),
    ("src/foo_test.go", True), ("web/a.spec.ts", True), ("conftest.py", True),
    ("testing/x.py", False), ("docs/tests.md", False), ("pkg/__tests__/a.js", True),
])
def test_test_paths_follow_common_conventions(path, expected):
    assert is_test_path(path) is expected


def test_the_decomposition_prompt_asks_for_final_text_and_a_split_estimate(tmp_path):
    prompts = []

    def invoke(key, prompt, **kw):
        prompts.append(prompt)
        return _declaration(FINAL)

    session = Session("fix", ArtifactStore(tmp_path / ".quadratus"), invoke,
                      config=SessionConfig(project=tmp_path, allow_writes=True))
    session.next_task()
    text = prompts[0]
    assert "Size the implementation and its executed proof together" in text
    assert "estimate implementation lines, proof lines" in text and '"estimate": {"implementation": 45' in text
    assert "never describe a delivery whose tests are only deferred" in text
    assert "The description is final text" in text
    assert "exactly one signature" in text and "verbatim" in text


# Attempt 3 quoted the CSV column headings in acceptance. `Start (s)` reads as
# a call with one argument, so a lint that took any backticked name(args) as a
# declaration was one wording away from rejecting a correct decomposition over
# a column name. A name is declared only where the description writes `def`.
ATTEMPT_3_ACCEPTANCE = [
    "`def _import_preview_rows(project, text):` exists in app.py and returns `(result, None)`",
    "Header names are matched case-insensitively after trim; a missing `Tag Type`, "
    "`Start (s)` or `End (s)` header yields `(None, (message, \"bad_request\"))`",
]
ATTEMPT_3_BODY = (
    "Add the core CSV-parsing helper as a pure function in `app.py`. Signature: "
    "`def _import_preview_rows(project, text):`. Required: `tag type`, `start (s)`, "
    "`end (s)`. Start/End: `float()` of the stripped cell, `math.isfinite`.\n"
)


def test_quoted_column_headings_are_not_declared_functions():
    found = declared_signatures(ATTEMPT_3_BODY, ["A helper `_import_preview_rows(project, text)`"],
                                ATTEMPT_3_ACCEPTANCE)
    assert found == {"_import_preview_rows": ["project, text"]}
    scope, _ = read_scope(_declaration(ATTEMPT_3_BODY, acceptance=ATTEMPT_3_ACCEPTANCE),
                          max_lines=100)
    assert scope.max_lines == 100


# -- f6-download-guard on 2ffa7f6: an elided call is not a signature -------------------

F6_BODY = (
    "Add a shared path guard.\n\n```python\ndef _serve_guarded(directory, name):\n"
    "    \"\"\"Serve name from directory or abort 403.\"\"\"\n```\n\n"
    "Both `/videos/<name>` and `/recordings/<name>` call `_serve_guarded(...)` and "
    "return its response unchanged; `_serve_guarded(\u2026)` must resolve the path "
    "before comparing.\n"
)


@pytest.mark.parametrize("mention", ["`_serve_guarded(...)`", "`_serve_guarded(\u2026)`",
                                     "`_serve_guarded( ... )`"])
def test_an_elided_call_is_not_a_second_signature(mention):
    body = "```python\ndef _serve_guarded(directory, name):\n```\nRoutes call " + mention + "."
    assert declared_signatures(body, [], []) == {"_serve_guarded": ["directory, name"]}


def test_the_f6_shape_passes_the_lint():
    acceptance = ["app.py defines `_serve_guarded(directory, name)`",
                  "`python -m pytest -q` passes"]
    scope, body = read_scope(_declaration(F6_BODY, acceptance=acceptance), max_lines=100)
    assert declared_signatures(body, [scope.intended_result], scope.acceptance) == {
        "_serve_guarded": ["directory, name"],
    }


def test_a_real_second_signature_next_to_an_elision_is_still_rejected():
    body = ("```python\ndef _serve_guarded(directory, name):\n```\nRoutes call "
            "`_serve_guarded(...)`; actually make it `_serve_guarded(directory, name, root)`.")
    with pytest.raises(ValueError) as info:
        read_scope(_declaration(body), max_lines=100)
    assert "(...)" not in str(info.value)
    assert "_serve_guarded(directory, name, root)" in str(info.value)


def test_a_signature_quoted_only_in_acceptance_needs_a_def_to_be_checked():
    # No `def` anywhere: nothing is declared, so nothing can contradict.
    body = "Call `helper(a, b)` and then `helper(a, b, c)` as convenient."
    assert declared_signatures(body, [], []) == {}


# -- declared review-only intent (Codex review of 3d5c3f3) -------------------------------

def test_review_only_is_declared_not_inferred_from_a_small_ceiling():
    from quadratus.scope import read_scope
    audit, _ = read_scope(_declaration(FINAL, max_lines=1, edits="none"), max_lines=100)
    assert audit.review_only is True and audit.to_dict()["edits"] == "none"
    assert "Declared review-only" in audit.render()
    one_line_fix, _ = read_scope(_declaration(FINAL, max_lines=1), max_lines=100)
    assert one_line_fix.review_only is False and "edits" not in one_line_fix.to_dict()
    explicit, _ = read_scope(_declaration(FINAL, edits="allowed"), max_lines=100)
    assert explicit.review_only is False
    for bad in ("some", True, None, ""):
        with pytest.raises(ValueError, match='edits must be "none"'):
            read_scope(_declaration(FINAL, edits=bad), max_lines=100)


# -- the optional SCOPE estimate: strict when given, unknown when not ------------------------

def test_an_estimate_is_read_strictly_rendered_and_kept_but_widens_nothing():
    estimate = {"implementation": 45, "proof": 45, "setup": 10}
    scope, _body = read_scope(_declaration(FINAL, max_lines=100, estimate=estimate), max_lines=100)
    assert scope.estimate == estimate and scope.max_lines == 100
    assert scope.to_dict()["estimate"] == estimate
    rendered = scope.render()
    assert "implementation 45, proof 45, setup 10" in rendered and "every line counts the same" in rendered
    # The measured bound is max_lines and its 1.5 stop, whatever the split says.
    diff = "+++ b/app.py\n" + "+x\n" * 93 + "+++ b/tests/test_app.py\n" + "+y\n" * 60
    report = scope.assess(diff)
    assert (report.changed_lines, report.code_lines, report.test_lines) == (153, 93, 60)
    assert report.oversized and report.max_lines == 100


@pytest.mark.parametrize("estimate, reason", [
    ({"implementation": 45, "proof": 45}, "exactly implementation, proof and setup"),
    ({"implementation": 45, "proof": 45, "setup": 10, "docs": 0}, "exactly implementation, proof and setup"),
    ({"implementation": True, "proof": 89, "setup": 10}, "non-negative integer"),
    ({"implementation": -5, "proof": 95, "setup": 10}, "non-negative integer"),
    ({"implementation": 45.0, "proof": 45, "setup": 10}, "non-negative integer"),
    ({"implementation": 40, "proof": 40, "setup": 10}, "must equal max_lines"),
    (["implementation", "proof", "setup"], "exactly implementation, proof and setup"),
])
def test_a_malformed_estimate_is_refused_not_read_as_a_lower_figure(estimate, reason):
    with pytest.raises(ValueError, match=reason):
        read_scope(_declaration(FINAL, max_lines=100, estimate=estimate), max_lines=100)


def test_a_declaration_without_an_estimate_is_unknown_and_unchanged():
    scope, _body = read_scope(_declaration(FINAL), max_lines=100)
    assert scope.estimate is None and "estimate" not in scope.to_dict()
    assert "planner's split" not in scope.render()
    assert TaskScope(max_lines=50).estimate is None


# -- the optional SCOPE proofs: strict scenarios, unknown when absent ------------------------

GOOD_PROOF = dict(id="P1", requirements=["R1"], operation="add(1, 2)", state="nothing stored",
                  expect="3 is returned", location="tests/test_import_preview.py")


def test_proof_scenarios_are_read_strictly_rendered_and_serialized():
    scope, _body = read_scope(_declaration(FINAL, proofs=[GOOD_PROOF]), max_lines=100)
    kept = dict(GOOD_PROOF, evidence="test")
    assert scope.proofs == (kept,) and scope.to_dict()["proofs"] == [kept], "the default modality is made explicit"
    rendered = scope.render()
    assert "Proof scenarios" in rendered and "P1 [R1] add(1, 2); given nothing stored; expect 3 is returned" in rendered
    assert "PROVES: P<n> - path/test_x.py::Class::test_case" in rendered


@pytest.mark.parametrize("proofs, reason", [
    ([], "non-empty list"),
    ({"id": "P1"}, "non-empty list"),
    ([dict(GOOD_PROOF, extra=1)], "exactly id, requirements"),
    ([{k: v for k, v in GOOD_PROOF.items() if k != "state"}], "exactly id, requirements"),
    ([dict(GOOD_PROOF, id="S1")], "unique P<n> ids"),
    ([GOOD_PROOF, dict(GOOD_PROOF)], "unique P<n> ids"),
    ([dict(GOOD_PROOF, requirements=[])], "distinct R<n> requirement ids"),
    ([dict(GOOD_PROOF, requirements=["R1", "R1"])], "distinct R<n> requirement ids"),
    ([dict(GOOD_PROOF, requirements="R1")], "distinct R<n> requirement ids"),
    ([dict(GOOD_PROOF, expect=" ")], "non-empty expect"),
    ([dict(GOOD_PROOF, location="/abs/test.py")], "narrow project-relative path"),
    ([dict(GOOD_PROOF, location="../tests/test_x.py")], "narrow project-relative path"),
    ([dict(GOOD_PROOF, evidence="docs")], "evidence must be one of test, reading, capture"),
    ([dict(GOOD_PROOF, evidence=None)], "evidence must be one of test, reading, capture"),
])
def test_a_malformed_proof_list_is_refused_whole(proofs, reason):
    with pytest.raises(ValueError, match=reason):
        read_scope(_declaration(FINAL, proofs=proofs), max_lines=100)


def test_a_declared_reading_modality_is_kept_verbatim_and_never_inferred_from_the_location():
    """A1: the modality is what the planner declared. A README scenario
    without ``evidence`` is a test scenario on the record (the default,
    written out, never inferred from the .md location); one declaring
    reading keeps it."""
    docs = dict(GOOD_PROOF, location="README.md")
    scope, _body = read_scope(_declaration(FINAL, proofs=[docs]), max_lines=100)
    assert scope.proofs == (dict(docs, evidence="test"),)
    reading = dict(docs, evidence="reading")
    scope, _body = read_scope(_declaration(FINAL, proofs=[reading]), max_lines=100)
    assert scope.proofs[0]["evidence"] == "reading" and scope.to_dict()["proofs"] == [reading]
    assert "reading" in scope.render()
    visual = dict(GOOD_PROOF, location="templates/index.html", evidence="capture")
    scope, _body = read_scope(_declaration(FINAL, proofs=[visual]), max_lines=100)
    assert scope.proofs[0]["evidence"] == "capture" and "evidence: capture" in scope.render()


def test_a_declaration_without_proofs_is_unknown_not_complete():
    scope, _body = read_scope(_declaration(FINAL), max_lines=100)
    assert scope.proofs is None and "proofs" not in scope.to_dict() and "Proof scenarios" not in scope.render()
