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
    assert "Estimate code lines and test lines separately" in text
    assert "The description is final text" in text
    assert "exactly one signature" in text and "verbatim" in text
