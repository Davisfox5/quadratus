"""The operator's outer path limit is a ceiling fixed at dispatch (map P3.4;
O-NEXT-10 E at 548da3d, Codex ruling 5865344590).

The scope check and the prompt read live ``default_scope`` on every call, and
the contract recorded only the task's own scope. A limit widened after
dispatch loosened the task; a tightened one restricted it, which is a safe
operator revocation and stays. The contract now carries the limit's identity
(``operator_limits``), the session holds the limit bound at dispatch, and
every scope check and prompt applies it together with a live limit that
differs, so the effective limit can only narrow. The difference is recorded.
Controller-level on a real Session, plus one whole-controller journey for
the final stop; the drift is a synthetic write, a controller invariant.
"""

import dataclasses

from quadratus.artifacts import ArtifactStore
from quadratus.outcome import TaskOutcome, missing_facts
from quadratus.scope import TaskScope
from quadratus.session import Session, SessionConfig, TaskSpec

WIDE = TaskScope(permitted_paths=["app.py", "README.md"], max_lines=200)
NARROW = TaskScope(permitted_paths=["README.md"], max_lines=200)
LIMITS = "Operator limits (also binding):"


def _session(tmp_path, limit):
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text("def add(a, b):\n    return 0\n")
    (root / "README.md").write_text("# app\n")
    prompts = []

    def invoke(key, prompt, *a, **k):
        prompts.append(prompt)
        return "Done.\nCHANGED: []"
    session = Session("goal", ArtifactStore(tmp_path / "a"), invoke,
                      config=SessionConfig(project=root, allow_writes=True, default_scope=limit))
    session._prompts = prompts
    return session


def _prompt(session):
    session._prompts.clear()
    session._invoke_model("claude:opus", "Revise.")
    (prompt,) = session._prompts
    return prompt


def _dispatch(session):
    spec = TaskSpec("t1", "Implement add in app.py.", kind="refactor",
                    scope=TaskScope(permitted_paths=["app.py", "README.md"], max_lines=40))
    outcome = TaskOutcome("t1", "implementation")
    session._outcome = outcome
    session._task_outer = session.config.default_scope
    session._contract = session._build_contract(spec, outcome)
    session._active_spec = spec
    session._task_before = session._capture_source()
    return spec


def _edit_app(session):
    (session.project / "app.py").write_text("def add(a, b):\n    return a + b\n")


def _drift(session, limit):
    session.config = dataclasses.replace(session.config, default_scope=limit)


def _identity(limit):
    return Session._limits_identity(TaskSpec("x", "x"), limit)


# -- a live limit may tighten a task ----------------------------------------------------

def test_a_limit_tightened_after_dispatch_still_restricts(tmp_path):
    session = _session(tmp_path, WIDE)
    spec = _dispatch(session)
    _edit_app(session)
    assert session._measure_scope(spec, session._task_before).out_of_scope == []
    _drift(session, NARROW)
    after = session._measure_scope(spec, session._task_before)
    assert after.out_of_scope == ["app.py"], "the operator's revocation holds"
    assert f"operator_limits: contract {_identity(WIDE)!r}, legacy {_identity(NARROW)!r}" \
        in session._outcome.mismatches


# -- ...and never loosen it -------------------------------------------------------------

def test_a_limit_widened_after_dispatch_does_not_loosen(tmp_path):
    session = _session(tmp_path, NARROW)
    spec = _dispatch(session)
    _edit_app(session)
    assert session._measure_scope(spec, session._task_before).out_of_scope == ["app.py"]
    _drift(session, WIDE)
    after = session._measure_scope(spec, session._task_before)
    assert after.out_of_scope == ["app.py"], "the dispatch limit is a ceiling"
    assert f"operator_limits: contract {_identity(NARROW)!r}, legacy {_identity(WIDE)!r}" \
        in session._outcome.mismatches


def test_a_widened_limit_keeps_the_ceiling_in_the_line_budget(tmp_path):
    session = _session(tmp_path, TaskScope(permitted_paths=["app.py", "README.md"], max_lines=1))
    spec = _dispatch(session)
    _drift(session, WIDE)
    assert session._measure_scope(spec, session._task_before).max_lines == 1


def test_the_prompt_names_the_dispatch_limit_and_a_tighter_live_one(tmp_path):
    session = _session(tmp_path, WIDE)
    _dispatch(session)
    _drift(session, NARROW)
    prompt = _prompt(session)
    assert prompt.count(LIMITS) == 2
    assert WIDE.render() in prompt and NARROW.render() in prompt


def test_the_prompt_keeps_the_dispatch_limit_when_the_live_one_is_removed(tmp_path):
    session = _session(tmp_path, NARROW)
    spec = _dispatch(session)
    _drift(session, None)
    prompt = _prompt(session)
    assert prompt.count(LIMITS) == 1 and NARROW.render() in prompt
    _edit_app(session)
    assert session._measure_scope(spec, session._task_before).out_of_scope == ["app.py"]
    assert f"operator_limits: contract {_identity(NARROW)!r}, legacy 'none'" in session._outcome.mismatches


# -- controls ---------------------------------------------------------------------------

def test_no_drift_applies_the_limit_once_with_no_mismatch(tmp_path):
    session = _session(tmp_path, NARROW)
    spec = _dispatch(session)
    _edit_app(session)
    assert session._measure_scope(spec, session._task_before).out_of_scope == ["app.py"]
    prompt = _prompt(session)
    assert prompt.count(LIMITS) == 1 and NARROW.render() in prompt
    assert session._outcome.mismatches == []
    assert session._contract.required.operator_limits == _identity(NARROW)


def test_no_operator_limit_is_none(tmp_path):
    session = _session(tmp_path, None)
    spec = _dispatch(session)
    _edit_app(session)
    assert session._measure_scope(spec, session._task_before).out_of_scope == []
    assert session._outer_limits(spec) == [] and session._outcome.mismatches == []
    assert session._contract.required.operator_limits == "none"


def test_a_task_whose_scope_is_the_limit_has_no_outer_limit(tmp_path):
    session = _session(tmp_path, WIDE)
    spec = TaskSpec("t1", "Implement add.", kind="refactor", scope=WIDE)
    outcome = TaskOutcome("t1", "implementation")
    session._outcome, session._task_outer = outcome, WIDE
    session._contract = session._build_contract(spec, outcome)
    assert session._contract.required.operator_limits == "none"
    assert session._outer_limits(spec) == [] and outcome.mismatches == []


def test_with_no_task_contract_the_live_limit_decides(tmp_path):
    session = _session(tmp_path, NARROW)
    spec = TaskSpec("t1", "Implement add.", kind="refactor", scope=TaskScope(permitted_paths=["app.py"]))
    assert [b.permitted_paths for b in session._outer_limits(spec)] == [NARROW.permitted_paths]
    _drift(session, WIDE)
    assert [b.permitted_paths for b in session._outer_limits(spec)] == [WIDE.permitted_paths]


# -- the record ---------------------------------------------------------------------

BASE = dict(checks=False, design_evidence="none", design_review=False, security_verification=False,
            settlement=False, design_collaboration_applicable=False, design_instruction="none",
            security_verdict="none")


def _record(required):
    from tests.test_outcome import _closed
    task = _closed()
    task.contract["required"] = required
    return task


def test_an_older_record_without_the_limit_says_so():
    assert missing_facts(_record(dict(BASE))) == [
        "t1.contract.required.operator_limits (absent: recorded before this field existed)"]


def test_a_malformed_limit_is_missing():
    assert missing_facts(_record(dict(BASE, operator_limits="README.md"))) == [
        "t1.contract.required.operator_limits"]
    assert missing_facts(_record(dict(BASE, operator_limits="sha256:" + "0" * 64))) == []


# -- whole controller: the final stop -----------------------------------------------------

def _journey(tmp_path, monkeypatch, *, widen):
    from quadratus import project_run
    from tests.lifecycle import harness as H
    from tests.lifecycle.test_lifecycle_matrix import DECL_T1, FILES, Script
    tmp_path.mkdir()
    tight = TaskScope(permitted_paths=["app.py", "tests/test_app.py"], max_lines=1)
    real = project_run.SessionConfig
    monkeypatch.setattr(project_run, "SessionConfig", lambda **kw: real(**{**kw, "default_scope": tight}))
    if widen:
        draft = Session._draft_with_channels

        def widened(self, *args, **kw):
            self.config = dataclasses.replace(
                self.config, default_scope=TaskScope(permitted_paths=["app.py", "tests/test_app.py"],
                                                     max_lines=200))
            return draft(self, *args, **kw)
        monkeypatch.setattr(Session, "_draft_with_channels", widened)
    replay = H.run(tmp_path, monkeypatch, Script(orchestrator=lambda call, replay: DECL_T1), files=FILES,
                   record_complete=False)
    return replay, next(t for t in replay.workflow["tasks"] if t["task_id"] == "t1")


def test_a_widened_limit_ends_the_task_as_the_dispatch_limit_would(tmp_path, monkeypatch):
    control, c1 = _journey(tmp_path / "control", monkeypatch, widen=False)
    replay, t1 = _journey(tmp_path / "drift", monkeypatch, widen=True)
    assert replay.result.error == control.result.error, "the same final stop as with no drift"
    assert t1["closed_as"] == c1["closed_as"]
    assert any(m.startswith("operator_limits: contract 'sha256:") for m in t1["mismatches"])
    assert not replay.result.completed
