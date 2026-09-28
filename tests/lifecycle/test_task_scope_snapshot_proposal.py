"""Proposed invariant: a dispatched task's own scope cannot widen in place.

These are synthetic post-dispatch mutations of a real Session's mutable
TaskScope. They are not an observed production trigger. The two strict xfails
pin the remaining behavior at a761edd; controls preserve no drift and live
operator tightening. This file changes no engine behavior.
"""

import json

import pytest

from quadratus.artifacts import ArtifactStore
from quadratus.outcome import TaskOutcome
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec


def _session(tmp_path, outer):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def add(a, b):\n    return 0\n")
    (root / "README.md").write_text("# app\n")
    prompts = []

    def invoke(key, prompt, **kw):
        prompts.append(prompt)
        return "Done.\nCHANGED: []"

    session = Session("goal", ArtifactStore(tmp_path / "artifacts"), invoke,
                      config=SessionConfig(project=root, allow_writes=True,
                                           default_scope=outer))
    return session, prompts


def _dispatch(session, scope):
    spec = TaskSpec("t1", "Implement add in app.py.", kind="refactor", scope=scope)
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._task_outer = session._snapshot_outer(spec)
    session._contract = session._build_contract(spec, outcome)
    session._active_spec = spec
    session._task_before = session._capture_source()
    return spec


def _edit_app(session):
    (session.project / "app.py").write_text("def add(a, b):\n    return a + b\n")


@pytest.mark.xfail(strict=True, reason="task scope is read live after dispatch")
def test_a_distinct_task_scope_cannot_widen_in_place(tmp_path):
    scope = TaskScope(permitted_paths=["README.md"], max_lines=1)
    outer = TaskScope(permitted_paths=["README.md", "app.py"], max_lines=200)
    session, _ = _session(tmp_path, outer)
    spec = _dispatch(session, scope)
    recorded = json.loads(session._contract.scope)
    assert recorded["permitted_paths"] == ["README.md"] and recorded["max_lines"] == 1
    scope.permitted_paths.append("app.py")
    scope.max_lines = 200
    _edit_app(session)
    report = session._measure_scope(spec, session._task_before)
    assert report.out_of_scope == ["app.py"] and report.max_lines == 1


@pytest.mark.xfail(strict=True, reason="aliased task and operator scope is read live")
def test_a_task_scope_aliasing_the_operator_limit_cannot_widen_in_place(tmp_path):
    shared = TaskScope(permitted_paths=["README.md"], max_lines=1)
    session, _ = _session(tmp_path, shared)
    spec = _dispatch(session, shared)
    assert session._task_outer is None
    assert session._contract.required.operator_limits == "none"
    assert json.loads(session._contract.scope)["permitted_paths"] == ["README.md"]
    shared.permitted_paths.append("app.py")
    shared.max_lines = 200
    _edit_app(session)
    report = session._measure_scope(spec, session._task_before)
    assert report.out_of_scope == ["app.py"] and report.max_lines == 1


@pytest.mark.xfail(strict=True, reason="task instructions render the live mutable scope")
def test_the_task_prompt_keeps_the_dispatched_scope(tmp_path):
    scope = TaskScope(permitted_paths=["README.md"], max_lines=1)
    session, prompts = _session(tmp_path, TaskScope(permitted_paths=["README.md", "app.py"], max_lines=200))
    _dispatch(session, scope)
    scope.permitted_paths.append("app.py")
    scope.max_lines = 200
    session._invoke_model("claude:opus", "Revise.")
    assert "You may change only these paths: README.md." in prompts[-1]
    assert "You may change only these paths: README.md, app.py." not in prompts[-1]


def test_no_drift_keeps_the_declared_scope_and_report(tmp_path):
    scope = TaskScope(permitted_paths=["README.md"], max_lines=1)
    session, prompts = _session(tmp_path, TaskScope(permitted_paths=["README.md", "app.py"], max_lines=200))
    spec = _dispatch(session, scope)
    _edit_app(session)
    report = session._measure_scope(spec, session._task_before)
    assert report.out_of_scope == ["app.py"] and report.max_lines == 1
    session._invoke_model("claude:opus", "Revise.")
    assert "You may change only these paths: README.md." in prompts[-1]


def test_live_operator_tightening_still_restricts_a_separate_task_scope(tmp_path):
    scope = TaskScope(permitted_paths=["README.md", "app.py"], max_lines=40)
    outer = TaskScope(permitted_paths=["README.md", "app.py"], max_lines=200)
    session, prompts = _session(tmp_path, outer)
    spec = _dispatch(session, scope)
    _edit_app(session)
    assert session._measure_scope(spec, session._task_before).out_of_scope == []
    outer.permitted_paths = ["README.md"]
    report = session._measure_scope(spec, session._task_before)
    assert report.out_of_scope == ["app.py"]
    session._invoke_model("claude:opus", "Revise.")
    assert "Operator limits (also binding):" in prompts[-1]
    assert any(m.startswith("operator_limits: contract ") for m in session._outcome.mismatches)
